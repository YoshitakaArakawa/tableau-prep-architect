"""Augment a Tableau Published Data Source with transforms and Calculated Fields.

Reads a spec JSON, applies transforms (rename / hide column / cast type via
hidden+calc) and/or injects <column><calculation/></column> elements into
the .tds XML, publishes, and re-downloads to verify the edits survived.

Source kinds:
- "extract" — existing extract-based PDS. Download with include_extract=True,
  edit XML, republish (CreateNew default / Overwrite optional).
- "live"    — existing live (federated/virtual-connection backed) PDS.
  Download with include_extract=False, edit XML, republish.
- "vconn"   — no existing source PDS. Caller provides vconn LUID + table ref +
  column list; this script builds a base .tds from scratch wrapping the vconn,
  applies transforms, publishes (CreateNew only). Used by tableau-prep-builder when the
  decomposed Prep flow's Input was a virtual connection and stg is to be
  materialized as a Live PDS without going through Prep.

Rename semantics (binding-layer contract):
- kind="vconn"        -> TRUE RENAME: local-name rewrite + <cols> map to the
  physical column. Required because downstream Prep flows (LoadSqlProxy) bind
  PDS fields by local-name; caption is display-only for BI/VizQL.
- kind="extract/live" -> caption-only. Safe for BI consumers of an existing
  PDS (true rename would break their field references), NOT sufficient when a
  downstream Prep flow reads the PDS. Renaming for Prep consumption on these
  kinds is unsupported - materialize the stg as a real .tfl instead.

See SKILL.md and references/tds-calc-field-format.md for the spec contract and
XML shape.

Usage:
    python augment_pds.py --spec spec.json --out-dir ./augment_out

Exit codes:
    0   Success (publish + verify both passed)
    1   Spec validation error / caption collision / unknown column reference
    2   HTTP error from Tableau (publish 4xx/5xx)
    3   Round-trip verification failed (transform or calc missing after publish)
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import sys
import time
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

# Repo root is 4 levels up from this script:
#   .claude/skills/tableau-pds-augmenter/scripts/augment_pds.py  ->  repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import signed_in_server  # noqa: E402

ALLOWED_DATATYPES = {"real", "integer", "string", "boolean", "date", "datetime"}
ALLOWED_ROLES = {"measure", "dimension"}
ALLOWED_TYPES = {"quantitative", "nominal", "ordinal"}
ALLOWED_SOURCE_KINDS = {"extract", "live", "vconn"}
ALLOWED_TRANSFORM_OPS = {"rename", "cast", "hide"}

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

# Default cast function per target datatype. Caller can override with
# transforms[].cast_formula for non-default expressions (e.g. parse string -> date
# with a specific format).
DEFAULT_CAST_FUNCTION_BY_DATATYPE = {
    "real": "FLOAT",
    "integer": "INT",
    "string": "STR",
    "date": "DATE",
    "datetime": "DATETIME",
    # boolean intentionally absent: no clean 1-arg cast function. Caller must
    # provide cast_formula explicitly if they need to derive a boolean.
}

# Defaults used only when source.kind='vconn' and the script must synthesize
# <metadata-record> entries from scratch. These are best-effort stubs aligned
# with what Tableau Cloud emits for a federated/publishedConnection source
# (observed in work/20260524_pds_live_stg/Sample Vconn Datasource.tdsx). If a
# vconn column needs a different remote-type or aggregation, the caller may
# override per-column via spec.source.columns[].remote_type / .aggregation.
DEFAULT_REMOTE_TYPE_BY_DATATYPE = {
    "string": "130",
    "integer": "20",
    "real": "5",
    "date": "7",
    "datetime": "135",
    "boolean": "11",
}
DEFAULT_AGGREGATION_BY_DATATYPE = {
    "string": "Count",
    "integer": "Sum",
    "real": "Sum",
    "date": "Year",
    "datetime": "Year",
    "boolean": "Count",
}


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------

class SpecError(ValueError):
    """Raised on malformed spec - caller must fix the spec."""


def _validate_transforms(transforms: list) -> list[dict]:
    """Validate each transform op and normalize fields. Returns normalized list."""
    normalized = []
    column_names_seen_per_op = {"rename": set(), "cast": set(), "hide": set()}
    for i, t in enumerate(transforms):
        if not isinstance(t, dict):
            raise SpecError(f"spec.transforms[{i}] must be an object")
        op = t.get("op")
        if op not in ALLOWED_TRANSFORM_OPS:
            raise SpecError(
                f"spec.transforms[{i}].op={op!r} not in {sorted(ALLOWED_TRANSFORM_OPS)}"
            )
        col = t.get("column_name")
        if not col or not isinstance(col, str) or not col.startswith("["):
            raise SpecError(
                f"spec.transforms[{i}].column_name must be a string starting with "
                f"'[' (the internal name attribute of the source <column>), got {col!r}"
            )
        if col in column_names_seen_per_op[op]:
            raise SpecError(
                f"spec.transforms[{i}]: column_name={col!r} already targeted by "
                f"an earlier op={op!r}"
            )
        column_names_seen_per_op[op].add(col)

        if op == "rename":
            if not t.get("to_caption"):
                raise SpecError(f"spec.transforms[{i}] (op=rename) requires to_caption")
            normalized.append({"op": "rename", "column_name": col, "to_caption": t["to_caption"]})
        elif op == "cast":
            if not t.get("to_caption"):
                raise SpecError(f"spec.transforms[{i}] (op=cast) requires to_caption")
            dt = t.get("to_datatype")
            if dt not in ALLOWED_DATATYPES:
                raise SpecError(
                    f"spec.transforms[{i}] (op=cast).to_datatype={dt!r} not in "
                    f"{sorted(ALLOWED_DATATYPES)}"
                )
            cast_formula = t.get("cast_formula")
            if not cast_formula:
                fn = DEFAULT_CAST_FUNCTION_BY_DATATYPE.get(dt)
                if not fn:
                    raise SpecError(
                        f"spec.transforms[{i}] (op=cast).to_datatype={dt!r} has no "
                        "default cast function; caller must supply cast_formula"
                    )
                cast_formula = f"{fn}({col})"
            role = t.get("role") or DEFAULT_ROLE_BY_DATATYPE[dt]
            if role not in ALLOWED_ROLES:
                raise SpecError(
                    f"spec.transforms[{i}] (op=cast).role={role!r} not in "
                    f"{sorted(ALLOWED_ROLES)}"
                )
            ctype = t.get("type") or DEFAULT_TYPE_BY_ROLE[role]
            if ctype not in ALLOWED_TYPES:
                raise SpecError(
                    f"spec.transforms[{i}] (op=cast).type={ctype!r} not in "
                    f"{sorted(ALLOWED_TYPES)}"
                )
            normalized.append({
                "op": "cast",
                "column_name": col,
                "to_caption": t["to_caption"],
                "to_datatype": dt,
                "cast_formula": cast_formula,
                "role": role,
                "type": ctype,
            })
        elif op == "hide":
            normalized.append({"op": "hide", "column_name": col})
    return normalized


def _validate_calcs(calcs: list) -> list[dict]:
    normalized = []
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
        norm = dict(c, role=role, type=ctype)
        if norm["caption"] in captions_seen:
            raise SpecError(
                f"spec.calcs[{i}].caption={norm['caption']!r} duplicates an earlier entry"
            )
        captions_seen.add(norm["caption"])
        normalized.append(norm)
    return normalized


def _validate_vconn_columns(cols) -> list[dict]:
    """Validate spec.source.columns[] for kind='vconn'. Returns normalized list."""
    if not isinstance(cols, list) or not cols:
        raise SpecError(
            "spec.source.columns must be a non-empty list when source.kind='vconn' "
            "(caller must enumerate every vconn-table column; auto-discovery is "
            "not done by this Skill)"
        )
    seen_names = set()
    seen_captions = set()
    norm = []
    for i, c in enumerate(cols):
        if not isinstance(c, dict):
            raise SpecError(f"spec.source.columns[{i}] must be an object")
        for k in ("name", "caption", "datatype"):
            if not c.get(k):
                raise SpecError(f"spec.source.columns[{i}].{k} is required")
        name = c["name"]
        if not isinstance(name, str) or not name.startswith("["):
            raise SpecError(
                f"spec.source.columns[{i}].name must be a string starting with "
                f"'[' (the internal bracket form), got {name!r}"
            )
        if name in seen_names:
            raise SpecError(
                f"spec.source.columns[{i}].name={name!r} duplicates an earlier column"
            )
        seen_names.add(name)
        if c["caption"] in seen_captions:
            raise SpecError(
                f"spec.source.columns[{i}].caption={c['caption']!r} duplicates an earlier column"
            )
        seen_captions.add(c["caption"])
        dt = c["datatype"]
        if dt not in ALLOWED_DATATYPES:
            raise SpecError(
                f"spec.source.columns[{i}].datatype={dt!r} not in {sorted(ALLOWED_DATATYPES)}"
            )
        role = c.get("role") or DEFAULT_ROLE_BY_DATATYPE[dt]
        if role not in ALLOWED_ROLES:
            raise SpecError(
                f"spec.source.columns[{i}].role={role!r} not in {sorted(ALLOWED_ROLES)}"
            )
        ctype = c.get("type") or DEFAULT_TYPE_BY_ROLE[role]
        if ctype not in ALLOWED_TYPES:
            raise SpecError(
                f"spec.source.columns[{i}].type={ctype!r} not in {sorted(ALLOWED_TYPES)}"
            )
        remote_name = c.get("remote_name") or name.strip("[]")
        norm.append({
            "name": name,
            "remote_name": remote_name,
            "caption": c["caption"],
            "datatype": dt,
            "role": role,
            "type": ctype,
            "remote_type": str(c.get("remote_type", DEFAULT_REMOTE_TYPE_BY_DATATYPE[dt])),
            "aggregation": c.get("aggregation", DEFAULT_AGGREGATION_BY_DATATYPE[dt]),
        })
    return norm


def load_and_validate_spec(spec_path: Path) -> dict:
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SpecError(f"spec root must be an object, got {type(raw).__name__}")

    src = raw.get("source") or {}
    if not isinstance(src, dict):
        raise SpecError("spec.source must be an object")
    kind = src.get("kind") or "extract"
    if kind not in ALLOWED_SOURCE_KINDS:
        raise SpecError(
            f"spec.source.kind={kind!r} not in {sorted(ALLOWED_SOURCE_KINDS)}"
        )

    # Required source fields differ by kind:
    #   extract/live -> source.luid (existing PDS)
    #   vconn        -> source.vconn_luid + table_uuid + table_name + columns[]
    source_luid = None
    vconn_fields = None
    if kind in {"extract", "live"}:
        if not src.get("luid"):
            raise SpecError(f"spec.source.luid is required when source.kind={kind!r}")
        source_luid = src["luid"]
    else:  # kind == "vconn"
        for k in ("vconn_luid", "table_uuid", "table_name"):
            if not src.get(k):
                raise SpecError(f"spec.source.{k} is required when source.kind='vconn'")
        vconn_columns = _validate_vconn_columns(src.get("columns"))
        vconn_fields = {
            "vconn_luid": src["vconn_luid"],
            "vconn_caption": src.get("vconn_caption") or src["vconn_luid"],
            "table_uuid": src["table_uuid"],
            "table_name": src["table_name"],
            "columns": vconn_columns,
        }

    tgt = raw.get("target") or {}
    if not isinstance(tgt, dict) or not tgt.get("new_name"):
        raise SpecError("spec.target.new_name is required")
    if kind == "vconn" and not tgt.get("project_id"):
        raise SpecError(
            "spec.target.project_id is required when source.kind='vconn' "
            "(no source PDS to inherit project from)"
        )

    mode = raw.get("mode") or "CreateNew"
    if mode not in {"CreateNew", "Overwrite"}:
        raise SpecError(f"spec.mode must be 'CreateNew' or 'Overwrite', got {mode!r}")
    if kind == "vconn" and mode == "Overwrite":
        raise SpecError(
            "spec.mode='Overwrite' is not supported when source.kind='vconn' "
            "(no source PDS to overwrite; use mode='CreateNew')"
        )

    transforms_raw = raw.get("transforms") or []
    calcs_raw = raw.get("calcs") or []
    if not isinstance(transforms_raw, list):
        raise SpecError("spec.transforms must be a list")
    if not isinstance(calcs_raw, list):
        raise SpecError("spec.calcs must be a list")
    if not transforms_raw and not calcs_raw:
        # vconn can publish a passthrough PDS without transforms (e.g. just
        # exposing the vconn table as a Live PDS without column edits). Allow.
        if kind != "vconn":
            raise SpecError(
                "spec must include at least one of transforms[] or calcs[] (both empty)"
            )

    transforms = _validate_transforms(transforms_raw) if transforms_raw else []
    calcs = _validate_calcs(calcs_raw) if calcs_raw else []

    # Cross-check: cast.to_caption / calcs.caption must not collide with each other.
    new_captions = [t["to_caption"] for t in transforms if t["op"] == "cast"] + [
        c["caption"] for c in calcs
    ]
    seen = set()
    for cap in new_captions:
        if cap in seen:
            raise SpecError(
                f"caption {cap!r} appears in both transforms[cast] and calcs[] "
                "(or twice across them) - must be globally unique among new visible columns"
            )
        seen.add(cap)

    return {
        "source_luid": source_luid,
        "source_kind": kind,
        "vconn": vconn_fields,
        "target_project_id": tgt.get("project_id"),
        "new_name": tgt["new_name"],
        "mode": mode,
        "transforms": transforms,
        "calcs": calcs,
    }


# ---------------------------------------------------------------------------
# .tdsx / .tds manipulation
# ---------------------------------------------------------------------------

# Regex matching one self-closing <column .../> declaration at the datasource
# level (single-line, single-quoted attrs - the .tds output format we observe
# from both Desktop and Cloud).
_COLUMN_RE = re.compile(r"<column (?P<attrs>[^>]+?)/>")


def extract_tds(tdsx: Path) -> tuple[str, bytes]:
    """Return (tds_entry_name, tds_bytes) from a .tdsx zip."""
    with zipfile.ZipFile(tdsx) as zf:
        name = next((n for n in zf.namelist() if n.lower().endswith(".tds")), None)
        if not name:
            raise RuntimeError(f"no .tds entry found in {tdsx}")
        return name, zf.read(name)


def visible_captions(tds_text: str) -> set[str]:
    """Captions present on <column ...> declarations whose hidden attr is NOT 'true'.

    Hidden columns are excluded because they do not occupy the consumer-visible
    caption namespace (Metadata API / VizQL drop them).
    """
    found = set()
    for m in re.finditer(r"<column[^>]*?/>", tds_text):
        attrs = m.group(0)
        if "hidden='true'" in attrs or 'hidden="true"' in attrs:
            continue
        cap_m = re.search(r"caption=(['\"])(.*?)\1", attrs)
        if cap_m:
            found.add(cap_m.group(2))
    return found


def column_internal_names(tds_text: str) -> set[str]:
    """All `name='[...]'` values present on top-level <column> declarations.

    Used to validate transforms reference real columns before publish.
    """
    found = set()
    for m in re.finditer(r"<column[^>]*?name=(['\"])(\[.*?\])\1", tds_text):
        found.add(m.group(2))
    return found


def patch_column_attrs(tds_text: str, target_name: str, **attr_replacements) -> str:
    """Within a self-closing <column ... /> matching name='<target_name>',
    update each given attribute (must already exist)."""
    hits = []

    def repl(m: re.Match) -> str:
        attrs = m.group("attrs")
        if f"name='{target_name}'" not in attrs:
            return m.group(0)
        new_attrs = attrs
        for k, v in attr_replacements.items():
            pat = rf"{k}='[^']*'"
            if not re.search(pat, new_attrs):
                raise RuntimeError(
                    f"attr {k!r} not found on column {target_name} - cannot patch"
                )
            new_attrs = re.sub(pat, f"{k}='{v}'", new_attrs, count=1)
        hits.append(target_name)
        return f"<column {new_attrs}/>"

    out = _COLUMN_RE.sub(repl, tds_text)
    if not hits:
        raise RuntimeError(f"column with name={target_name!r} not found in .tds")
    return out


def add_hidden_attr(tds_text: str, target_name: str) -> str:
    """Add hidden='true' to the matching <column ... />. Idempotent."""
    hits = []

    def repl(m: re.Match) -> str:
        attrs = m.group("attrs")
        if f"name='{target_name}'" not in attrs:
            return m.group(0)
        if re.search(r"hidden='[^']*'", attrs):
            new_attrs = re.sub(r"hidden='[^']*'", "hidden='true'", attrs)
        else:
            new_attrs = attrs.rstrip() + " hidden='true' "
        hits.append(target_name)
        return f"<column {new_attrs}/>"

    out = _COLUMN_RE.sub(repl, tds_text)
    if not hits:
        raise RuntimeError(f"column with name={target_name!r} not found in .tds")
    return out


def build_calc_xml(calc: dict, calc_name: str) -> str:
    """Build the <column><calculation/></column> snippet for one calc spec."""
    quote_map = {"'": "&apos;", '"': "&quot;"}
    caption = xml_escape(calc["caption"], quote_map)
    formula = xml_escape(calc["formula"], quote_map)
    return (
        f"  <column caption='{caption}' datatype='{calc['datatype']}' name='[{calc_name}]' "
        f"role='{calc['role']}' type='{calc['type']}'>\n"
        f"    <calculation class='tableau' formula='{formula}' />\n"
        f"  </column>\n"
    )


def inject_into_tds(tds_text: str, calc_blocks: list[str]) -> str:
    """Insert calc XML after <aliases /> or before </datasource>."""
    if not calc_blocks:
        return tds_text
    payload = "".join(calc_blocks)
    aliases_marker = "<aliases enabled='yes' />"
    if aliases_marker in tds_text:
        return tds_text.replace(aliases_marker, aliases_marker + "\n" + payload, 1)
    close_marker = "</datasource>"
    if close_marker not in tds_text:
        raise RuntimeError("source .tds has no </datasource> closing tag - cannot inject")
    return tds_text.replace(close_marker, payload + close_marker, 1)


def rezip_tdsx(src_tdsx: Path, tds_entry_name: str, new_tds_bytes: bytes, dst_tdsx: Path):
    """Copy all entries from src_tdsx into dst_tdsx, replacing the .tds payload."""
    with zipfile.ZipFile(src_tdsx) as zin, zipfile.ZipFile(dst_tdsx, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = new_tds_bytes if item.filename == tds_entry_name else zin.read(item.filename)
            zout.writestr(item, data)


# ---------------------------------------------------------------------------
# vconn-source base .tds construction (kind='vconn')
# ---------------------------------------------------------------------------

def build_base_tds_from_vconn(vconn: dict) -> tuple[str, str]:
    """Construct a base .tds for a vconn-backed live PDS from scratch.

    Template shape mirrors a published vconn-backed PDS observed in
    work/20260524_pds_live_stg/Sample Vconn Datasource.tdsx (federated
    connection wrapping a <connection class='publishedConnection'>).

    Returns (ds_opaque_name, tds_text). ds_opaque_name is the formatted-name
    suffix (without the 'federated.' prefix) - the same string is used to name
    the .tds entry inside the wrapping .tdsx.

    Cloud will re-introspect metadata-records on publish if the connection is
    healthy, so the stubs we synthesize here are best-effort and only need to
    parse cleanly.
    """
    ds_opaque = secrets.token_hex(16)
    pc_opaque = secrets.token_hex(16)
    object_hex = uuid.uuid4().hex.upper()

    quote_map = {"'": "&apos;", '"': "&quot;"}
    vc_cap_esc = xml_escape(vconn["vconn_caption"], quote_map)
    table_name = vconn["table_name"]
    table_name_attr = xml_escape(table_name, quote_map)
    table_name_text = xml_escape(table_name)
    safe_object_basename = re.sub(r"[^A-Za-z0-9_]", "_", table_name)
    object_id = f"{safe_object_basename}_{object_hex}"
    named_conn_name = f"publishedConnection.{pc_opaque}"
    table_ref = f"[{vconn['table_uuid']}].[{table_name_attr}]"

    md_records = []
    for c in vconn["columns"]:
        collation = (
            "<collation flag='1' name='LEN_RUS_S2' />"
            if c["datatype"] == "string"
            else "<collation flag='0' name='LROOT' />"
        )
        md_records.append(
            "      <metadata-record class='column'>\n"
            f"        <remote-name>{xml_escape(c['remote_name'])}</remote-name>\n"
            f"        <remote-type>{c['remote_type']}</remote-type>\n"
            f"        <local-name>{c['name']}</local-name>\n"
            f"        <parent-name>[{table_name_text}]</parent-name>\n"
            f"        <remote-alias>{xml_escape(c['remote_name'])}</remote-alias>\n"
            f"        <caption>{xml_escape(c['caption'])}</caption>\n"
            f"        <local-type>{c['datatype']}</local-type>\n"
            f"        <aggregation>{c['aggregation']}</aggregation>\n"
            "        <contains-null>true</contains-null>\n"
            f"        {collation}\n"
            f"        <object-id>[{object_id}]</object-id>\n"
            "      </metadata-record>"
        )

    col_decls = []
    for c in vconn["columns"]:
        col_decls.append(
            f"  <column caption='{xml_escape(c['caption'], quote_map)}' "
            f"datatype='{c['datatype']}' name='{c['name']}' "
            f"role='{c['role']}' type='{c['type']}' />"
        )

    tds = (
        "<?xml version='1.0' encoding='utf-8' ?>\n"
        "\n"
        f"<datasource formatted-name='federated.{ds_opaque}' inline='true' "
        f"source-platform='linux' version='18.1' "
        f"xmlns:user='http://www.tableausoftware.com/xml/user'>\n"
        "  <document-format-change-manifest>\n"
        "    <ObjectModelEncapsulateLegacy />\n"
        "    <ObjectModelTableType />\n"
        "    <SchemaViewerObjectModel />\n"
        "  </document-format-change-manifest>\n"
        "  <connection class='federated'>\n"
        "    <named-connections>\n"
        f"      <named-connection caption='{vc_cap_esc}' name='{named_conn_name}'>\n"
        f"        <connection class='publishedConnection' "
        f"resourceId='{vconn['vconn_luid']}' resourceName='{vc_cap_esc}' />\n"
        "      </named-connection>\n"
        "    </named-connections>\n"
        f"    <relation connection='{named_conn_name}' name='{table_name_attr}' "
        f"table='{table_ref}' type='table' />\n"
        "    <metadata-records>\n"
        + "\n".join(md_records) + "\n"
        "    </metadata-records>\n"
        "  </connection>\n"
        "  <aliases enabled='yes' />\n"
        + "\n".join(col_decls) + "\n"
        f"  <column caption='{table_name_attr}' datatype='table' "
        f"name='[__tableau_internal_object_id__].[{object_id}]' "
        f"role='measure' type='quantitative' />\n"
        "  <layout dim-ordering='alphabetic' measure-ordering='alphabetic' "
        "show-structure='true' />\n"
        "  <object-graph>\n"
        "    <objects>\n"
        f"      <object caption='{table_name_attr}' id='{object_id}'>\n"
        "        <properties context=''>\n"
        f"          <relation connection='{named_conn_name}' name='{table_name_attr}' "
        f"table='{table_ref}' type='table' />\n"
        "        </properties>\n"
        "      </object>\n"
        "    </objects>\n"
        "  </object-graph>\n"
        "</datasource>\n"
    )
    return ds_opaque, tds


def wrap_tds_as_tdsx(tds_text: str, tds_entry_name: str, out_path: Path) -> None:
    """Pack a single .tds string into a minimal .tdsx zip at out_path."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(tds_entry_name, tds_text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Transform pipeline
# ---------------------------------------------------------------------------

def apply_true_renames(
    tds_text: str,
    rename_ops: list[dict],
    columns: list[dict],
    table_name: str,
) -> str:
    """Rewrite rename targets at the local-name layer (vconn sources only).

    Downstream Prep flows (LoadSqlProxy) bind PDS fields by local-name; the
    caption is display-only for BI/VizQL. A caption-only rename therefore
    leaves downstream Prep seeing the physical (e.g. UUID) names. This
    function performs the "true rename":

      1. every bracketed occurrence of the old name (`<local-name>` in
         metadata-records + the `name` attr on `<column>`) becomes
         `[to_caption]`
      2. remote-name / remote-alias keep the physical identity
      3. a `<cols><map key='[to_caption]' value='[table].[remote]'/></cols>`
         block after `</metadata-records>` maps logical -> physical (the
         same mechanism Desktop emits when logical and remote names differ)
    """
    remote_by_name = {c["name"]: c["remote_name"] for c in columns}
    out = tds_text
    maps: list[str] = []
    for t in rename_ops:
        old, new_cap = t["column_name"], t["to_caption"]
        if "[" in new_cap or "]" in new_cap:
            raise SpecError(
                f"to_caption={new_cap!r} must not contain brackets (it becomes "
                "the field's local-name under vconn true-rename)"
            )
        new_local = f"[{new_cap}]"
        if new_local in out:
            raise SpecError(
                f"true-rename target {new_local} already exists in the .tds - "
                "caption collides with an existing local-name"
            )
        if old not in out:
            raise SpecError(f"true-rename source {old} not found in the .tds")
        remote = remote_by_name.get(old)
        if remote is None:
            raise SpecError(
                f"rename target {old} has no matching source.columns[] entry - "
                "cannot derive the physical remote-name for the <cols> map"
            )
        out = out.replace(old, new_local)
        maps.append(f"      <map key='{new_local}' value='[{table_name}].[{xml_escape(remote)}]' />")

    marker = "</metadata-records>\n"
    if marker not in out:
        raise SpecError("source .tds has no </metadata-records> - cannot insert <cols> map")
    cols_block = "    <cols>\n" + "\n".join(maps) + "\n    </cols>\n"
    return out.replace(marker, marker + cols_block, 1)


def apply_transforms(
    tds_text: str,
    transforms: list[dict],
    base_calc_id: int,
    vconn_ctx: dict | None = None,
) -> tuple[str, list[dict], list[tuple[str, str]]]:
    """Apply rename / cast / hide transforms to the .tds in place.

    `vconn_ctx` ({"columns": [...], "table_name": str}) switches rename ops to
    true-rename semantics (local-name rewrite + <cols> map) - required for a
    stg PDS that downstream Prep flows will read. Without it (download-based
    extract/live sources) renames stay caption-only, which is safe for PDSes
    consumed by BI but NOT sufficient for downstream Prep consumption.

    Returns:
        - new tds text (with renames, hides, and cast-source-hides applied)
        - list of synthetic calc dicts produced by cast ops (to be injected
          alongside user-provided calcs)
        - list of (cast.column_name, generated calc_name) for verification
    """
    valid_names = column_internal_names(tds_text)
    for t in transforms:
        if t["column_name"] not in valid_names:
            raise SpecError(
                f"transforms references column_name={t['column_name']!r} which is "
                "not present as a top-level <column> in the source .tds. Check the "
                "exact `name` attribute (case-sensitive, brackets included)."
            )

    rename_ops = [t for t in transforms if t["op"] == "rename"]
    if vconn_ctx and rename_ops:
        # true-rename rewrites the name attr, so a cast/hide on the same
        # column would reference a name that no longer exists afterwards.
        overlap = {t["column_name"] for t in rename_ops} & {
            t["column_name"] for t in transforms if t["op"] in ("cast", "hide")
        }
        if overlap:
            raise SpecError(
                f"columns {sorted(overlap)} are targeted by rename AND cast/hide - "
                "unsupported under vconn true-rename (rename the cast output "
                "caption instead, or drop the rename)"
            )

    out = tds_text
    synthetic_calcs: list[dict] = []
    cast_calc_names: list[tuple[str, str]] = []

    # Apply renames first. Caption is always updated; under vconn_ctx the
    # local-name layer is rewritten afterwards (true rename).
    for i, t in enumerate(transforms):
        if t["op"] != "rename":
            continue
        out = patch_column_attrs(out, t["column_name"], caption=t["to_caption"])

    # Apply hides next.
    for t in transforms:
        if t["op"] != "hide":
            continue
        out = add_hidden_attr(out, t["column_name"])

    # Apply casts: hide the source column + materialize a synthetic calc spec
    # that the existing calc-injection pipeline will append.
    for i, t in enumerate(transforms):
        if t["op"] != "cast":
            continue
        out = add_hidden_attr(out, t["column_name"])
        # Stagger calc IDs by index so casts and user calcs do not collide.
        # User calcs will start from base_calc_id + len(transforms_casts).
        calc_name = f"Calculation_{base_calc_id + len(synthetic_calcs)}"
        synthetic_calcs.append({
            "caption": t["to_caption"],
            "formula": t["cast_formula"],
            "datatype": t["to_datatype"],
            "role": t["role"],
            "type": t["type"],
            "_source_op": "cast",
            "_cast_source_column": t["column_name"],
        })
        cast_calc_names.append((t["column_name"], calc_name))

    # Finally rewrite the local-name layer for vconn sources. Runs last so
    # hide/cast above operate on the original (physical) names.
    if vconn_ctx and rename_ops:
        out = apply_true_renames(
            out, rename_ops, vconn_ctx["columns"], vconn_ctx["table_name"]
        )

    return out, synthetic_calcs, cast_calc_names


# ---------------------------------------------------------------------------
# Round-trip verification
# ---------------------------------------------------------------------------

def verify_calc_present(verify_text: str, calc: dict, calc_name: str) -> dict:
    id_present = f"[{calc_name}]" in verify_text
    caption_present = calc["caption"] in verify_text
    tokens = re.findall(r"\[[^\]]+\]|[A-Z]{2,}\(?", calc["formula"])
    operands_present = all(t.rstrip("(") in verify_text for t in tokens) if tokens else True
    return {
        "id_present": id_present,
        "caption_present": caption_present,
        "formula_operands_present": operands_present,
    }


def verify_transform_applied(verify_text: str, t: dict, true_rename: bool = False) -> dict:
    """Per-transform survival check on the re-DL'd .tds.

    For cast/hide: confirm the source column has hidden='true'.
    For rename (caption-only): confirm the new caption appears on the target column.
    For rename (true_rename): the column is looked up by its NEW local-name
    `[to_caption]` - survival of the renamed name is the gate; the <cols> map's
    presence is reported as info (the server may normalize it away on re-introspection).
    """
    col_name = t["column_name"]
    if true_rename and t["op"] == "rename":
        col_name = f"[{t['to_caption']}]"
    # Find the matching <column> in the verified .tds.
    col_match = None
    for m in _COLUMN_RE.finditer(verify_text):
        if f"name='{col_name}'" in m.group("attrs"):
            col_match = m.group(0)
            break
    found = col_match is not None
    out = {"op": t["op"], "column_name": col_name, "column_found_in_verify": found}
    if t["op"] == "rename" and true_rename:
        out["semantics"] = "true_rename"
        out["map_present"] = f"key='{col_name}'" in verify_text
    if not found:
        return out
    if t["op"] == "rename":
        out["caption_is_new"] = f"caption='{t['to_caption']}'" in col_match
    elif t["op"] == "hide":
        out["hidden_true"] = "hidden='true'" in col_match
    elif t["op"] == "cast":
        out["source_hidden_true"] = "hidden='true'" in col_match
    return out


def transform_pass(result: dict) -> bool:
    if not result["column_found_in_verify"]:
        return False
    if result["op"] == "rename":
        if result.get("semantics") == "true_rename":
            # The renamed local-name surviving the publish round-trip is the
            # binding-layer guarantee downstream Prep needs.
            return True
        return bool(result.get("caption_is_new"))
    if result["op"] == "hide":
        return bool(result.get("hidden_true"))
    if result["op"] == "cast":
        return bool(result.get("source_hidden_true"))
    return False


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

    kind = spec["source_kind"]
    if kind == "vconn":
        vc = spec["vconn"]
        print(
            f"[spec] kind=vconn  vconn_luid={vc['vconn_luid']}  table={vc['table_name']!r}  "
            f"columns={len(vc['columns'])}  new_name={spec['new_name']}  mode={spec['mode']}"
        )
    else:
        print(
            f"[spec] source_luid={spec['source_luid']}  kind={kind}  "
            f"new_name={spec['new_name']}  mode={spec['mode']}"
        )
    print(f"[spec] transforms: {len(spec['transforms'])}  calcs: {len(spec['calcs'])}")

    with signed_in_server() as server:
        # 1. Obtain base .tdsx
        if kind == "vconn":
            # Build base .tds from scratch wrapping the vconn - no DL required.
            ds_opaque, base_tds_text = build_base_tds_from_vconn(spec["vconn"])
            tds_entry = f"federated.{ds_opaque}.tds"
            original_tdsx = out_dir / "original.tdsx"
            wrap_tds_as_tdsx(base_tds_text, tds_entry, original_tdsx)
            print(
                f"[build] original.tdsx ({original_tdsx.stat().st_size} B) "
                f"synthesized from vconn (no source DL)"
            )
            original_tds_text = base_tds_text
            (out_dir / "original.tds").write_text(original_tds_text, encoding="utf-8")
        else:
            include_extract = kind == "extract"
            src = server.datasources.get_by_id(spec["source_luid"])
            print(
                f"[src] name={src.name}  project={src.project_name}  "
                f"type={src.datasource_type}"
            )

            if spec["mode"] == "Overwrite" and src.name != spec["new_name"]:
                print(
                    f"[spec] ERROR: mode=Overwrite requires source.name ({src.name!r}) == "
                    f"new_name ({spec['new_name']!r})",
                    file=sys.stderr,
                )
                sys.exit(1)

            tmp_dl = out_dir / "_dl_orig"
            tmp_dl.mkdir(exist_ok=True)
            dl_path = Path(server.datasources.download(
                src.id, filepath=str(tmp_dl), include_extract=include_extract
            ))
            original_tdsx = out_dir / "original.tdsx"
            shutil.move(str(dl_path), original_tdsx)
            shutil.rmtree(tmp_dl)
            print(
                f"[dl] original.tdsx ({original_tdsx.stat().st_size} B)  "
                f"include_extract={include_extract}"
            )

            tds_entry, tds_bytes = extract_tds(original_tdsx)
            original_tds_text = tds_bytes.decode("utf-8")
            (out_dir / "original.tds").write_text(original_tds_text, encoding="utf-8")

        # 2. Apply transforms, then inject calcs

        # Apply transforms first - this rewrites <column> attrs and hides source
        # columns for cast ops, producing synthetic calc specs for the cast ops.
        base_id = int(time.time() * 1000)
        # vconn sources get true-rename semantics: the synthesized PDS is new,
        # so no BI content depends on the old names, and downstream Prep flows
        # require the rename at the local-name layer.
        vconn_ctx = (
            {"columns": spec["vconn"]["columns"], "table_name": spec["vconn"]["table_name"]}
            if kind == "vconn"
            else None
        )
        if vconn_ctx and any(t["op"] == "rename" for t in spec["transforms"]):
            print("[transform] rename semantics: true_rename (vconn source)")
        try:
            post_transform_text, synthetic_calcs, cast_calc_pairs = apply_transforms(
                original_tds_text, spec["transforms"], base_id, vconn_ctx=vconn_ctx
            )
        except SpecError as e:
            print(f"[transform] ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        # Collision detection now runs on the post-transform visible caption space
        # (hidden columns are excluded). All new captions from cast ops + user calcs
        # must not collide with what's left visible.
        existing_visible = visible_captions(post_transform_text)
        new_captions = [c["caption"] for c in synthetic_calcs] + [
            c["caption"] for c in spec["calcs"]
        ]
        collisions = [cap for cap in new_captions if cap in existing_visible]
        if collisions:
            print(
                f"[collision] caption(s) {collisions} collide with visible columns "
                "after transforms. Use rename or hide ops on the conflicting source "
                "columns, or change the new caption(s).",
                file=sys.stderr,
            )
            sys.exit(1)

        # Materialize calcs: cast-synthetic calcs first (using IDs from
        # apply_transforms), then user calcs (IDs continue from there).
        cast_count = len(synthetic_calcs)
        user_calc_names = [
            f"Calculation_{base_id + cast_count + i}" for i in range(len(spec["calcs"]))
        ]
        all_calc_pairs = list(
            zip(synthetic_calcs, [n for _, n in cast_calc_pairs])
        ) + list(zip(spec["calcs"], user_calc_names))

        calc_blocks = [build_calc_xml(c, n) for c, n in all_calc_pairs]
        edited_text = inject_into_tds(post_transform_text, calc_blocks)
        (out_dir / "edited.tds").write_text(edited_text, encoding="utf-8")

        edited_tdsx = out_dir / "edited.tdsx"
        rezip_tdsx(original_tdsx, tds_entry, edited_text.encode("utf-8"), edited_tdsx)
        print(
            f"[edit] edited.tdsx ({edited_tdsx.stat().st_size} B)  "
            f"cast_calcs={len(synthetic_calcs)} user_calcs={len(spec['calcs'])}"
        )

        # 3. Publish
        # For kind=vconn, target.project_id is validated as required above.
        # For extract/live, fall back to the source PDS's project.
        target_project_id = spec["target_project_id"] or (
            src.project_id if kind != "vconn" else None
        )
        if not target_project_id:
            print(
                "[publish] ERROR: target_project_id could not be resolved",
                file=sys.stderr,
            )
            sys.exit(1)
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
        v_path = Path(server.datasources.download(
            published.id, filepath=str(tmp_v), include_extract=False
        ))
        verify_tdsx = out_dir / "verified.tdsx"
        shutil.move(str(v_path), verify_tdsx)
        shutil.rmtree(tmp_v)
        _, v_bytes = extract_tds(verify_tdsx)
        verify_text = v_bytes.decode("utf-8")
        (out_dir / "verified.tds").write_text(verify_text, encoding="utf-8")

        # 4a. Verify transforms
        transform_results = [
            verify_transform_applied(verify_text, t, true_rename=vconn_ctx is not None)
            for t in spec["transforms"]
        ]
        transforms_ok = all(transform_pass(r) for r in transform_results)
        for t, r in zip(spec["transforms"], transform_results):
            mark = "OK " if transform_pass(r) else "MISS"
            print(f"[verify-tx] {mark} op={t['op']} column={t['column_name']} {r}")

        # 4b. Verify all calcs (cast-synthetic + user-provided)
        calc_results = []
        calcs_ok = True
        for (c, name) in all_calc_pairs:
            r = verify_calc_present(verify_text, c, name)
            r["caption"] = c["caption"]
            r["calc_name"] = name
            r["source_op"] = c.get("_source_op", "calc")
            calc_results.append(r)
            ok = r["id_present"] and r["caption_present"] and r["formula_operands_present"]
            mark = "OK " if ok else "MISS"
            print(
                f"[verify-calc] {mark} src={r['source_op']} caption={c['caption']!r} "
                f"id={name} {r}"
            )
            calcs_ok = calcs_ok and ok

        all_ok = transforms_ok and calcs_ok
        result_payload = {
            "published_luid": published.id,
            "published_name": published.name,
            "project_id": target_project_id,
            "mode": spec["mode"],
            "source_kind": spec["source_kind"],
            "rename_semantics": "true_rename" if vconn_ctx else "caption_only",
            "transforms_applied": len(spec["transforms"]),
            "calcs_injected": len(all_calc_pairs),
            "verified": all_ok,
            "transforms": transform_results,
            "calcs": calc_results,
            "next_step_recommendation": (
                "For VizQL-layer verification (especially that cast ops actually "
                "expose the new type), run mcp__tableau__get-datasource-metadata "
                f"on luid={published.id} and assert each cast op's to_caption "
                "appears with the expected dataType + columnClass=CALCULATION."
            ),
        }
        print(f"RESULT_JSON: {json.dumps(result_payload, ensure_ascii=False)}")
        if not all_ok:
            sys.exit(3)


if __name__ == "__main__":
    main()
