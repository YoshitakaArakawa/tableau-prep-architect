"""Augment a Tableau Published Data Source with Calculated Fields.

Reads a spec JSON, downloads the source PDS as .tdsx, injects
<column><calculation/></column> elements into the .tds XML, republishes (CreateNew
or Overwrite), and re-downloads to verify the calc fields survived the round-trip.

See SKILL.md and references/tds-calc-field-format.md for the spec contract and XML
shape.

Usage:
    python augment_pds.py --spec spec.json --out-dir ./augment_out

Exit codes:
    0   Success (publish + verify both passed)
    1   Spec validation error / caption collision / missing fields
    2   HTTP error from Tableau (publish 4xx/5xx)
    3   Round-trip verification failed (calc missing after publish)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

# Repo root is 4 levels up from this script:
#   .claude/skills/prep-pds-augmenter/scripts/augment_pds.py  →  repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import sign_in_server  # noqa: E402

# Tableau .tds <column> attribute allowlists.
# datatype: other values exist (e.g. spatial) but are out of initial scope.
# role / type: the canonical Desktop categories — anything else is rejected
# upstream so the publish call does not silently accept malformed XML.
ALLOWED_DATATYPES = {"real", "integer", "string", "boolean", "date", "datetime"}
ALLOWED_ROLES = {"measure", "dimension"}
ALLOWED_TYPES = {"quantitative", "nominal", "ordinal"}

# Defaults pick the Desktop convention so the caller only has to supply
# caption / formula / datatype for the common case.
DEFAULT_ROLE_BY_DATATYPE = {
    "real": "measure",
    "integer": "measure",
    "string": "dimension",
    "boolean": "dimension",
    "date": "dimension",
    "datetime": "dimension",
}
DEFAULT_TYPE_BY_ROLE = {
    "measure": "quantitative",
    "dimension": "nominal",
}


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------

class SpecError(ValueError):
    """Raised on malformed spec — caller must fix the spec."""


def load_and_validate_spec(spec_path: Path) -> dict:
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SpecError(f"spec root must be an object, got {type(raw).__name__}")

    src = raw.get("source") or {}
    if not isinstance(src, dict) or not src.get("luid"):
        raise SpecError("spec.source.luid is required")

    tgt = raw.get("target") or {}
    if not isinstance(tgt, dict) or not tgt.get("new_name"):
        raise SpecError("spec.target.new_name is required")

    mode = raw.get("mode") or "CreateNew"
    if mode not in {"CreateNew", "Overwrite"}:
        raise SpecError(f"spec.mode must be 'CreateNew' or 'Overwrite', got {mode!r}")

    calcs = raw.get("calcs")
    if not isinstance(calcs, list) or not calcs:
        raise SpecError("spec.calcs must be a non-empty list")

    captions_seen = set()
    for i, c in enumerate(calcs):
        if not isinstance(c, dict):
            raise SpecError(f"spec.calcs[{i}] must be an object")
        for k in ("caption", "formula", "datatype"):
            if not c.get(k):
                raise SpecError(f"spec.calcs[{i}].{k} is required")
        if c["datatype"] not in ALLOWED_DATATYPES:
            raise SpecError(
                f"spec.calcs[{i}].datatype={c['datatype']!r} not in {sorted(ALLOWED_DATATYPES)}"
            )
        role = c.get("role") or DEFAULT_ROLE_BY_DATATYPE[c["datatype"]]
        if role not in ALLOWED_ROLES:
            raise SpecError(f"spec.calcs[{i}].role={role!r} not in {sorted(ALLOWED_ROLES)}")
        ctype = c.get("type") or DEFAULT_TYPE_BY_ROLE[role]
        if ctype not in ALLOWED_TYPES:
            raise SpecError(f"spec.calcs[{i}].type={ctype!r} not in {sorted(ALLOWED_TYPES)}")
        c["role"] = role
        c["type"] = ctype
        cap = c["caption"]
        if cap in captions_seen:
            raise SpecError(f"spec.calcs[{i}].caption={cap!r} duplicates an earlier entry")
        captions_seen.add(cap)

    return {
        "source_luid": src["luid"],
        "target_project_id": tgt.get("project_id"),
        "new_name": tgt["new_name"],
        "mode": mode,
        "calcs": calcs,
    }


# ---------------------------------------------------------------------------
# .tdsx / .tds manipulation
# ---------------------------------------------------------------------------

def extract_tds(tdsx: Path) -> tuple[str, bytes]:
    """Return (tds_entry_name, tds_bytes) from a .tdsx zip."""
    with zipfile.ZipFile(tdsx) as zf:
        name = next((n for n in zf.namelist() if n.lower().endswith(".tds")), None)
        if not name:
            raise RuntimeError(f"no .tds entry found in {tdsx}")
        return name, zf.read(name)


def existing_captions(tds_text: str) -> set[str]:
    """Caption values present in existing <column ...> declarations.

    Used to detect collisions before injection. Catches both quote styles.
    """
    found = set()
    for m in re.finditer(r"<column[^>]*\bcaption=(['\"])(.*?)\1", tds_text):
        found.add(m.group(2))
    return found


def build_calc_xml(calc: dict, calc_name: str) -> str:
    """Build the <column><calculation/></column> snippet for one calc spec.

    Attributes use single quotes, matching Sample - Superstore.tds. Caller-provided
    strings are XML-escaped via xml_escape; the escape function targets `& < >` and
    we pass quote-escaping kwargs to also handle `' "` since attribute values use
    single quotes here.
    """
    quote_map = {"'": "&apos;", '"': "&quot;"}
    caption = xml_escape(calc["caption"], quote_map)
    formula = xml_escape(calc["formula"], quote_map)
    datatype = calc["datatype"]
    role = calc["role"]
    ctype = calc["type"]
    return (
        f"  <column caption='{caption}' datatype='{datatype}' name='[{calc_name}]' "
        f"role='{role}' type='{ctype}'>\n"
        f"    <calculation class='tableau' formula='{formula}' />\n"
        f"  </column>\n"
    )


def inject_into_tds(tds_text: str, calc_blocks: list[str]) -> str:
    """Insert calc XML after <aliases /> or before </datasource>."""
    payload = "".join(calc_blocks)
    aliases_marker = "<aliases enabled='yes' />"
    if aliases_marker in tds_text:
        return tds_text.replace(aliases_marker, aliases_marker + "\n" + payload, 1)
    close_marker = "</datasource>"
    if close_marker not in tds_text:
        raise RuntimeError("source .tds has no </datasource> closing tag — cannot inject")
    return tds_text.replace(close_marker, payload + close_marker, 1)


def rezip_tdsx(src_tdsx: Path, tds_entry_name: str, new_tds_bytes: bytes, dst_tdsx: Path):
    """Copy all entries from src_tdsx into dst_tdsx, replacing the .tds payload."""
    with zipfile.ZipFile(src_tdsx) as zin, zipfile.ZipFile(dst_tdsx, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = new_tds_bytes if item.filename == tds_entry_name else zin.read(item.filename)
            zout.writestr(item, data)


# ---------------------------------------------------------------------------
# Round-trip verification
# ---------------------------------------------------------------------------

def verify_calc_present(verify_text: str, calc: dict, calc_name: str) -> dict:
    """Return dict of {id, caption, formula_operands} survival flags."""
    id_present = f"[{calc_name}]" in verify_text
    caption_present = calc["caption"] in verify_text
    # Check that key tokens of the formula survived (the formula itself may be
    # XML-escaped differently on round-trip). Heuristic: extract column refs `[name]`
    # and function tokens that look like SUM / COUNTD / IF etc. — all of these should
    # appear as substrings somewhere in the verified .tds.
    tokens = re.findall(r"\[[^\]]+\]|[A-Z]{2,}\(?", calc["formula"])
    operands_present = all(t.rstrip("(") in verify_text for t in tokens) if tokens else True
    return {
        "id_present": id_present,
        "caption_present": caption_present,
        "formula_operands_present": operands_present,
    }


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--spec", required=True, help="Path to spec JSON")
    p.add_argument("--out-dir", required=True, help="Directory for local artifacts")
    args = p.parse_args()

    spec_path = Path(args.spec).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        spec = load_and_validate_spec(spec_path)
    except SpecError as e:
        print(f"[spec] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[spec] source_luid={spec['source_luid']}  new_name={spec['new_name']}  mode={spec['mode']}")
    print(f"[spec] calcs: {len(spec['calcs'])}")

    server, auth = sign_in_server()
    with server.auth.sign_in(auth):
        # 1. Resolve source PDS and download
        src = server.datasources.get_by_id(spec["source_luid"])
        print(f"[src] name={src.name}  project={src.project_name}  type={src.datasource_type}")

        if spec["mode"] == "Overwrite" and src.name != spec["new_name"]:
            print(
                f"[spec] ERROR: mode=Overwrite requires source.name ({src.name!r}) == "
                f"new_name ({spec['new_name']!r})",
                file=sys.stderr,
            )
            sys.exit(1)

        # The TSC download() picks the filename; move it to a stable path.
        tmp_dl = out_dir / "_dl_orig"
        tmp_dl.mkdir(exist_ok=True)
        dl_path = Path(server.datasources.download(src.id, filepath=str(tmp_dl), include_extract=True))
        original_tdsx = out_dir / "original.tdsx"
        shutil.move(str(dl_path), original_tdsx)
        shutil.rmtree(tmp_dl)
        print(f"[dl] original.tdsx ({original_tdsx.stat().st_size} B)")

        # 2. Extract .tds, check caption collisions, inject
        tds_entry, tds_bytes = extract_tds(original_tdsx)
        original_tds_text = tds_bytes.decode("utf-8")
        (out_dir / "original.tds").write_text(original_tds_text, encoding="utf-8")

        existing = existing_captions(original_tds_text)
        collisions = [c["caption"] for c in spec["calcs"] if c["caption"] in existing]
        if collisions:
            print(
                f"[collision] caption(s) already exist in source .tds: {collisions}. "
                "Caller must change the caption(s) and retry.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Assign opaque calc names; stagger by 1ms per calc to keep them unique.
        base_id = int(time.time() * 1000)
        calc_names = [f"Calculation_{base_id + i}" for i in range(len(spec["calcs"]))]
        calc_blocks = [build_calc_xml(c, n) for c, n in zip(spec["calcs"], calc_names)]
        edited_text = inject_into_tds(original_tds_text, calc_blocks)
        (out_dir / "edited.tds").write_text(edited_text, encoding="utf-8")

        edited_tdsx = out_dir / "edited.tdsx"
        rezip_tdsx(original_tdsx, tds_entry, edited_text.encode("utf-8"), edited_tdsx)
        print(f"[edit] edited.tdsx ({edited_tdsx.stat().st_size} B)  calc_names={calc_names}")

        # 3. Publish
        target_project_id = spec["target_project_id"] or src.project_id
        ds_item = TSC.DatasourceItem(project_id=target_project_id, name=spec["new_name"])
        publish_mode = (
            TSC.Server.PublishMode.Overwrite
            if spec["mode"] == "Overwrite"
            else TSC.Server.PublishMode.CreateNew
        )
        try:
            published = server.datasources.publish(ds_item, str(edited_tdsx), mode=publish_mode)
        except TSC.ServerResponseError as e:
            print(f"[publish] HTTP {e.code}: {e.summary}: {e.detail}", file=sys.stderr)
            sys.exit(2)
        print(f"[publish] OK: name={published.name}  luid={published.id}")

        # 4. Verify by re-downloading
        tmp_v = out_dir / "_dl_verify"
        tmp_v.mkdir(exist_ok=True)
        v_path = Path(server.datasources.download(published.id, filepath=str(tmp_v), include_extract=False))
        verify_tdsx = out_dir / "verified.tdsx"
        shutil.move(str(v_path), verify_tdsx)
        shutil.rmtree(tmp_v)
        _, v_bytes = extract_tds(verify_tdsx)
        verify_text = v_bytes.decode("utf-8")
        (out_dir / "verified.tds").write_text(verify_text, encoding="utf-8")

        results = []
        all_ok = True
        for calc, name in zip(spec["calcs"], calc_names):
            r = verify_calc_present(verify_text, calc, name)
            r["caption"] = calc["caption"]
            r["calc_name"] = name
            results.append(r)
            ok = r["id_present"] and r["caption_present"] and r["formula_operands_present"]
            mark = "OK " if ok else "MISS"
            print(f"[verify] {mark} caption={calc['caption']!r} id={name} {r}")
            all_ok = all_ok and ok

        result_payload = {
            "published_luid": published.id,
            "published_name": published.name,
            "project_id": target_project_id,
            "mode": spec["mode"],
            "calcs_injected": len(spec["calcs"]),
            "verified": all_ok,
            "calcs": results,
        }
        print(f"RESULT_JSON: {json.dumps(result_payload, ensure_ascii=False)}")
        if not all_ok:
            sys.exit(3)


if __name__ == "__main__":
    main()
