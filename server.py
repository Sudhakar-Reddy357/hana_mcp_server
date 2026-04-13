"""
SAP HANA MCP Server
====================
A Model Context Protocol (MCP) server for SAP HANA development and analysis.
Provides tools for schema management, package browsing, calculation view operations,
table/view inspection, stored procedure management, system info, and custom query execution.

Transport: Server-Sent Events (SSE) via Starlette + Uvicorn.
"""

import os
import json
import logging
from typing import Annotated

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
import hdbcli.dbapi

from pydantic import Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("e_hana_mcp_server")

# ---------------------------------------------------------------------------
# MCP Server Instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "e_hana_mcp_server",
    description="SAP HANA MCP Server – development & analysis tools for SAP HANA",
)

# ---------------------------------------------------------------------------
# Connection State
# ---------------------------------------------------------------------------
# Holds the active hdbcli connection and metadata about configured systems.
_systems: dict[str, dict] = {}   # alias -> {address, port, user, password}
_active_system: str | None = None
_connection: hdbcli.dbapi.Connection | None = None
_current_schema: str | None = None


def _load_systems_from_env() -> None:
    """
    Populate the ``_systems`` dict from environment variables.
    
    Expected env‑var pattern (pipe‑separated list):
        HANA_SYSTEMS = "DEV|dev-host|39015|DEV_USER|pass1,QA|qa-host|39015|QA_USER|pass2"

    Falls back to a single system built from the legacy variables
    HANA_ADDRESS / HANA_PORT / HANA_USER / HANA_PASSWORD.
    """
    global _systems
    raw = os.environ.get("HANA_SYSTEMS", "")
    if raw:
        for entry in raw.split(","):
            parts = entry.strip().split("|")
            if len(parts) == 5:
                alias, address, port, user, password = parts
                _systems[alias.strip()] = {
                    "address": address.strip(),
                    "port": int(port.strip()),
                    "user": user.strip(),
                    "password": password.strip(),
                }
    else:
        # Fallback: single system from legacy env vars
        _systems["DEFAULT"] = {
            "address": os.environ.get("HANA_ADDRESS", "localhost"),
            "port": int(os.environ.get("HANA_PORT", "39015")),
            "user": os.environ.get("HANA_USER", "SYSTEM"),
            "password": os.environ.get("HANA_PASSWORD", ""),
        }


def _get_connection() -> hdbcli.dbapi.Connection:
    """Return the active hdbcli connection or raise."""
    if _connection is None:
        raise RuntimeError(
            "No active HANA connection. Use the 'connect_system' or "
            "'switch_system' tool first."
        )
    return _connection


def _connect(alias: str) -> str:
    """Connect to the system identified by *alias* and return a status message."""
    global _connection, _active_system, _current_schema
    if alias not in _systems:
        return f"Unknown system alias '{alias}'. Available: {list(_systems.keys())}"
    cfg = _systems[alias]
    try:
        if _connection is not None:
            try:
                _connection.close()
            except Exception:
                pass
        _connection = hdbcli.dbapi.connect(
            address=cfg["address"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
        )
        _active_system = alias
        # Determine current schema
        cursor = _connection.cursor()
        cursor.execute("SELECT CURRENT_SCHEMA FROM DUMMY")
        _current_schema = cursor.fetchone()[0]
        cursor.close()
        logger.info("Connected to %s (%s:%s)", alias, cfg["address"], cfg["port"])
        return (
            f"Connected to system '{alias}' at {cfg['address']}:{cfg['port']}. "
            f"Current schema: {_current_schema}"
        )
    except Exception as exc:
        _connection = None
        _active_system = None
        return f"Failed to connect to '{alias}': {exc}"


def _rows_to_table(columns: list[str], rows: list) -> str:
    """Format rows as a simple markdown table for readability."""
    if not rows:
        return "_No rows returned._"
    col_widths = [len(c) for c in columns]
    str_rows = []
    for row in rows:
        str_row = [str(v) if v is not None else "NULL" for v in row]
        for i, v in enumerate(str_row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(v))
        str_rows.append(str_row)
    header = "| " + " | ".join(c.ljust(col_widths[i]) for i, c in enumerate(columns)) + " |"
    sep = "|-" + "-|-".join("-" * col_widths[i] for i in range(len(columns))) + "-|"
    body_lines = []
    for sr in str_rows:
        line = "| " + " | ".join(sr[i].ljust(col_widths[i]) if i < len(col_widths) else sr[i] for i in range(len(sr))) + " |"
        body_lines.append(line)
    return "\n".join([header, sep] + body_lines)


# ===================================================================
# TOOL DEFINITIONS
# ===================================================================

# -------------------------------------------------------------------
# 1. System / Connection Management
# -------------------------------------------------------------------

@mcp.tool()
async def list_systems() -> str:
    """List all configured SAP HANA systems available for connection."""
    if not _systems:
        return "No systems configured. Set HANA_SYSTEMS or HANA_ADDRESS env vars."
    lines = [f"**Configured HANA Systems** (active → `{_active_system or 'none'}`):\n"]
    for alias, cfg in _systems.items():
        marker = " ✅" if alias == _active_system else ""
        lines.append(f"- **{alias}**{marker} — `{cfg['address']}:{cfg['port']}` (user: `{cfg['user']}`)")
    return "\n".join(lines)


@mcp.tool()
async def connect_system(
    alias: Annotated[str, Field(description="Alias of the HANA system to connect to (from list_systems)")]
) -> str:
    """Connect to a specific SAP HANA system by its alias."""
    return _connect(alias)


@mcp.tool()
async def switch_system(
    alias: Annotated[str, Field(description="Alias of the target HANA system to switch to")]
) -> str:
    """Disconnect from the current SAP HANA system and connect to another one."""
    return _connect(alias)


@mcp.tool()
async def get_current_system() -> str:
    """Return details about the currently active SAP HANA connection."""
    if _active_system is None:
        return "No system is currently connected."
    cfg = _systems[_active_system]
    return (
        f"**Active System:** `{_active_system}`\n"
        f"- Address: `{cfg['address']}:{cfg['port']}`\n"
        f"- User: `{cfg['user']}`\n"
        f"- Current Schema: `{_current_schema}`"
    )


# -------------------------------------------------------------------
# 2. Schema Management
# -------------------------------------------------------------------

@mcp.tool()
async def list_schemas() -> str:
    """List all schemas in the connected SAP HANA database."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT SCHEMA_NAME, SCHEMA_OWNER FROM SYS.SCHEMAS ORDER BY SCHEMA_NAME"
    )
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return _rows_to_table(cols, rows)


@mcp.tool()
async def set_schema(
    schema_name: Annotated[str, Field(description="Name of the schema to set as the current working schema")]
) -> str:
    """Change the current working schema on the active HANA connection."""
    global _current_schema
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(f'SET SCHEMA "{schema_name}"')
    _current_schema = schema_name
    cursor.close()
    return f"Current schema changed to `{schema_name}`."


# -------------------------------------------------------------------
# 3. Package Management (HANA Repository)
# -------------------------------------------------------------------

@mcp.tool()
async def list_packages(
    parent_package: Annotated[str | None, Field(description="Optional parent package path to filter children. Leave empty for root packages.")] = None
) -> str:
    """List SAP HANA repository packages. Optionally filter by a parent package path."""
    conn = _get_connection()
    cursor = conn.cursor()
    if parent_package:
        cursor.execute(
            "SELECT PACKAGE_ID, SRC_SYSTEM, SRC_TENANT, CDATA "
            "FROM _SYS_REPO.ACTIVE_OBJECT "
            "WHERE PACKAGE_ID LIKE ? AND OBJECT_SUFFIX = '' "
            "ORDER BY PACKAGE_ID",
            (f"{parent_package}.%",),
        )
    else:
        cursor.execute(
            "SELECT DISTINCT PACKAGE_ID "
            "FROM _SYS_REPO.ACTIVE_OBJECT "
            "ORDER BY PACKAGE_ID"
        )
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return _rows_to_table(cols, rows)


@mcp.tool()
async def get_package_objects(
    package_id: Annotated[str, Field(description="Full package path, e.g. 'mypackage.sub'")]
) -> str:
    """List all design-time objects inside a specific HANA repository package."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT OBJECT_NAME, OBJECT_SUFFIX, OBJECT_VERSION, ACTIVATED_AT "
        "FROM _SYS_REPO.ACTIVE_OBJECT "
        "WHERE PACKAGE_ID = ? AND OBJECT_SUFFIX != '' "
        "ORDER BY OBJECT_SUFFIX, OBJECT_NAME",
        (package_id,),
    )
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    if not rows:
        return f"No objects found in package `{package_id}`."
    return _rows_to_table(cols, rows)


# -------------------------------------------------------------------
# 4. Table & View Exploration
# -------------------------------------------------------------------

@mcp.tool()
async def list_tables(
    schema_name: Annotated[str | None, Field(description="Schema name to filter tables. Defaults to current schema.")] = None,
    filter_pattern: Annotated[str | None, Field(description="Optional LIKE pattern to filter table names, e.g. '%SALES%'.")] = None,
) -> str:
    """List tables in a given schema (or the current schema)."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    query = (
        "SELECT TABLE_NAME, TABLE_TYPE, RECORD_COUNT, TABLE_SIZE "
        "FROM SYS.M_TABLES WHERE SCHEMA_NAME = ?"
    )
    params: list = [schema]
    if filter_pattern:
        query += " AND TABLE_NAME LIKE ?"
        params.append(filter_pattern)
    query += " ORDER BY TABLE_NAME"
    cursor.execute(query, params)
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return _rows_to_table(cols, rows)


@mcp.tool()
async def list_views(
    schema_name: Annotated[str | None, Field(description="Schema name. Defaults to current schema.")] = None,
    filter_pattern: Annotated[str | None, Field(description="Optional LIKE pattern, e.g. '%ORDER%'.")] = None,
) -> str:
    """List SQL views in a given schema (or the current schema)."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    query = (
        "SELECT VIEW_NAME, VIEW_TYPE, IS_VALID "
        "FROM SYS.VIEWS WHERE SCHEMA_NAME = ?"
    )
    params: list = [schema]
    if filter_pattern:
        query += " AND VIEW_NAME LIKE ?"
        params.append(filter_pattern)
    query += " ORDER BY VIEW_NAME"
    cursor.execute(query, params)
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return _rows_to_table(cols, rows)


@mcp.tool()
async def get_table_metadata(
    table_name: Annotated[str, Field(description="Name of the table")],
    schema_name: Annotated[str | None, Field(description="Schema name. Defaults to current schema.")] = None,
) -> str:
    """Return column-level metadata for a table (column name, data type, length, nullable, default)."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, SCALE, IS_NULLABLE, DEFAULT_VALUE "
        "FROM SYS.TABLE_COLUMNS "
        "WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? "
        "ORDER BY POSITION",
        (schema, table_name),
    )
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    if not rows:
        return f"Table `{schema}.{table_name}` not found or has no columns."
    return f"**Columns of `{schema}.{table_name}`:**\n\n" + _rows_to_table(cols, rows)


@mcp.tool()
async def get_table_row_count(
    table_name: Annotated[str, Field(description="Name of the table")],
    schema_name: Annotated[str | None, Field(description="Schema name. Defaults to current schema.")] = None,
) -> str:
    """Get the row count for a specific table."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    cursor.execute(f'SELECT COUNT(*) AS ROW_COUNT FROM "{schema}"."{table_name}"')
    count = cursor.fetchone()[0]
    cursor.close()
    return f"Table `{schema}.{table_name}` has **{count:,}** rows."


@mcp.tool()
async def preview_table_data(
    table_name: Annotated[str, Field(description="Name of the table or view")],
    schema_name: Annotated[str | None, Field(description="Schema name. Defaults to current schema.")] = None,
    row_limit: Annotated[int, Field(description="Maximum number of rows to return (default 10, max 100)")] = 10,
) -> str:
    """Preview the first N rows of a table or view for quick data inspection."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    limit = min(row_limit, 100)
    cursor = conn.cursor()
    cursor.execute(f'SELECT TOP {limit} * FROM "{schema}"."{table_name}"')
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return f"**Preview of `{schema}.{table_name}` (top {limit}):**\n\n" + _rows_to_table(cols, rows)


# -------------------------------------------------------------------
# 5. Calculation Views
# -------------------------------------------------------------------

@mcp.tool()
async def list_calculation_views(
    filter_pattern: Annotated[str | None, Field(description="Optional LIKE pattern for view names, e.g. '%SALES%'.")] = None,
) -> str:
    """List all activated Calculation Views (from _SYS_BI.BIMC_CUBES)."""
    conn = _get_connection()
    cursor = conn.cursor()
    query = (
        "SELECT CATALOG_NAME, SCHEMA_NAME, CUBE_NAME, CUBE_TYPE, CREATE_TIME "
        "FROM _SYS_BI.BIMC_CUBES"
    )
    params: list = []
    if filter_pattern:
        query += " WHERE CUBE_NAME LIKE ?"
        params.append(filter_pattern)
    query += " ORDER BY CUBE_NAME"
    cursor.execute(query, params) if params else cursor.execute(query)
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return _rows_to_table(cols, rows)


@mcp.tool()
async def get_calculation_view_columns(
    view_name: Annotated[str, Field(description="Full name of the calculation view (e.g. 'package.path/CV_NAME')")],
) -> str:
    """Return the column metadata for a specific Calculation View."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COLUMN_NAME, COLUMN_SQL_TYPE, CUBE_NAME "
        "FROM _SYS_BI.BIMC_ALL_CUBES_COLUMNS "
        "WHERE CUBE_NAME LIKE ? "
        "ORDER BY COLUMN_NAME",
        (f"%{view_name}%",),
    )
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    if not rows:
        return f"No columns found for Calculation View matching `{view_name}`."
    return _rows_to_table(cols, rows)


@mcp.tool()
async def upload_calculation_view(
    package_id: Annotated[str, Field(description="Target HANA package path, e.g. 'mypackage.subpkg'")],
    object_name: Annotated[str, Field(description="Name for the Calculation View object")],
    xml_content: Annotated[str, Field(description="The full XML content of the Calculation View definition")],
) -> str:
    """
    Upload (create or update) a Calculation View definition in the HANA repository.
    The view must be activated separately after upload.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        # Check if object already exists
        cursor.execute(
            "SELECT COUNT(*) FROM _SYS_REPO.ACTIVE_OBJECT "
            "WHERE PACKAGE_ID = ? AND OBJECT_NAME = ? AND OBJECT_SUFFIX = 'calculationview'",
            (package_id, object_name),
        )
        exists = cursor.fetchone()[0] > 0

        if exists:
            # Update existing
            cursor.execute(
                "UPDATE _SYS_REPO.ACTIVE_OBJECT SET CDATA = ? "
                "WHERE PACKAGE_ID = ? AND OBJECT_NAME = ? AND OBJECT_SUFFIX = 'calculationview'",
                (xml_content, package_id, object_name),
            )
        else:
            # Insert new
            cursor.execute(
                "INSERT INTO _SYS_REPO.ACTIVE_OBJECT "
                "(PACKAGE_ID, OBJECT_NAME, OBJECT_SUFFIX, CDATA) "
                "VALUES (?, ?, 'calculationview', ?)",
                (package_id, object_name, xml_content),
            )
        conn.commit()
        action = "updated" if exists else "created"
        cursor.close()
        return (
            f"Calculation View `{package_id}::{object_name}` {action} successfully.\n"
            f"**Note:** You still need to activate it for it to take effect."
        )
    except Exception as exc:
        cursor.close()
        return f"Error uploading Calculation View: {exc}"


# -------------------------------------------------------------------
# 6. Stored Procedures
# -------------------------------------------------------------------

@mcp.tool()
async def list_procedures(
    schema_name: Annotated[str | None, Field(description="Schema name. Defaults to current schema.")] = None,
    filter_pattern: Annotated[str | None, Field(description="Optional LIKE pattern, e.g. '%PROC%'.")] = None,
) -> str:
    """List stored procedures in a given schema."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    query = (
        "SELECT PROCEDURE_NAME, PROCEDURE_TYPE, IS_VALID, CREATE_TIME "
        "FROM SYS.PROCEDURES WHERE SCHEMA_NAME = ?"
    )
    params: list = [schema]
    if filter_pattern:
        query += " AND PROCEDURE_NAME LIKE ?"
        params.append(filter_pattern)
    query += " ORDER BY PROCEDURE_NAME"
    cursor.execute(query, params)
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return _rows_to_table(cols, rows)


@mcp.tool()
async def get_procedure_definition(
    procedure_name: Annotated[str, Field(description="Name of the stored procedure")],
    schema_name: Annotated[str | None, Field(description="Schema name. Defaults to current schema.")] = None,
) -> str:
    """Return the SQL definition of a stored procedure."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DEFINITION FROM SYS.PROCEDURES "
        "WHERE SCHEMA_NAME = ? AND PROCEDURE_NAME = ?",
        (schema, procedure_name),
    )
    row = cursor.fetchone()
    cursor.close()
    if not row:
        return f"Procedure `{schema}.{procedure_name}` not found."
    return f"**Definition of `{schema}.{procedure_name}`:**\n\n```sql\n{row[0]}\n```"


# -------------------------------------------------------------------
# 7. System Information & Analysis
# -------------------------------------------------------------------

@mcp.tool()
async def get_system_overview() -> str:
    """Return SAP HANA system overview: version, memory, CPU, uptime, etc."""
    conn = _get_connection()
    cursor = conn.cursor()
    info_parts = []

    # Version
    cursor.execute("SELECT VALUE FROM SYS.M_SYSTEM_OVERVIEW WHERE NAME = 'Version'")
    row = cursor.fetchone()
    if row:
        info_parts.append(f"- **Version:** {row[0]}")

    # Instance info
    cursor.execute(
        "SELECT HOST, VALUE FROM SYS.M_SYSTEM_OVERVIEW WHERE SECTION = 'System' ORDER BY NAME"
    )
    for r in cursor.fetchall():
        info_parts.append(f"- {r[0]}: {r[1]}")

    # Memory
    cursor.execute(
        "SELECT ROUND(FREE_PHYSICAL_MEMORY/1024/1024/1024, 2) AS FREE_GB, "
        "ROUND(USED_PHYSICAL_MEMORY/1024/1024/1024, 2) AS USED_GB "
        "FROM SYS.M_HOST_RESOURCE_UTILIZATION"
    )
    row = cursor.fetchone()
    if row:
        info_parts.append(f"- **Memory Free:** {row[0]} GB")
        info_parts.append(f"- **Memory Used:** {row[1]} GB")

    cursor.close()
    return "**SAP HANA System Overview:**\n\n" + "\n".join(info_parts)


@mcp.tool()
async def get_table_disk_size(
    table_name: Annotated[str, Field(description="Name of the table")],
    schema_name: Annotated[str | None, Field(description="Schema name. Defaults to current schema.")] = None,
) -> str:
    """Get disk and memory size for a specific table."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    cursor.execute(
        "SELECT TABLE_NAME, RECORD_COUNT, "
        "ROUND(TABLE_SIZE/1024/1024, 2) AS SIZE_MB, "
        "ROUND(MEMORY_SIZE_IN_TOTAL/1024/1024, 2) AS MEMORY_MB "
        "FROM SYS.M_TABLES WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?",
        (schema, table_name),
    )
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    if not rows:
        return f"Table `{schema}.{table_name}` not found."
    return _rows_to_table(cols, rows)


@mcp.tool()
async def analyze_table_statistics(
    table_name: Annotated[str, Field(description="Name of the table")],
    schema_name: Annotated[str | None, Field(description="Schema name. Defaults to current schema.")] = None,
) -> str:
    """Analyze column-level statistics for a table: distinct values, nulls, min, max."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    # Get columns first
    cursor.execute(
        "SELECT COLUMN_NAME, DATA_TYPE_NAME FROM SYS.TABLE_COLUMNS "
        "WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? ORDER BY POSITION",
        (schema, table_name),
    )
    columns = cursor.fetchall()
    if not columns:
        cursor.close()
        return f"Table `{schema}.{table_name}` not found."

    stats_lines = [f"**Column Statistics for `{schema}.{table_name}`:**\n"]
    stats_lines.append("| Column | Type | Distinct | Nulls |")
    stats_lines.append("|--------|------|----------|-------|")

    for col_name, data_type in columns:
        try:
            cursor.execute(
                f'SELECT COUNT(DISTINCT "{col_name}") AS dist, '
                f'SUM(CASE WHEN "{col_name}" IS NULL THEN 1 ELSE 0 END) AS nulls '
                f'FROM "{schema}"."{table_name}"'
            )
            row = cursor.fetchone()
            dist = f"{row[0]:,}" if row else "?"
            nulls = f"{row[1]:,}" if row else "?"
        except Exception:
            dist = "N/A"
            nulls = "N/A"
        stats_lines.append(f"| {col_name} | {data_type} | {dist} | {nulls} |")

    cursor.close()
    return "\n".join(stats_lines)


@mcp.tool()
async def get_active_sessions() -> str:
    """List currently active database sessions."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT CONNECTION_ID, USER_NAME, CLIENT_HOST, CLIENT_IP, "
        "CONNECTION_STATUS, CREATED_BY "
        "FROM SYS.M_CONNECTIONS "
        "WHERE CONNECTION_STATUS = 'RUNNING' "
        "ORDER BY CONNECTION_ID"
    )
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return "**Active Sessions:**\n\n" + _rows_to_table(cols, rows)


@mcp.tool()
async def get_expensive_statements(
    top_n: Annotated[int, Field(description="Number of top expensive statements to return (default 10)")] = 10,
) -> str:
    """Return the top N most expensive SQL statements by total execution time."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT TOP ? STATEMENT_STRING, EXECUTION_COUNT, "
        "ROUND(TOTAL_EXECUTION_TIME/1000000, 2) AS TOTAL_SEC, "
        "ROUND(AVG_EXECUTION_TIME/1000000, 4) AS AVG_SEC, "
        "TOTAL_LOCK_WAIT_COUNT "
        "FROM SYS.M_SQL_PLAN_CACHE "
        "ORDER BY TOTAL_EXECUTION_TIME DESC",
        (top_n,),
    )
    cols = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    cursor.close()
    return f"**Top {top_n} Expensive Statements:**\n\n" + _rows_to_table(cols, rows)


# -------------------------------------------------------------------
# 8. Development Tools
# -------------------------------------------------------------------

@mcp.tool()
async def create_table(
    table_name: Annotated[str, Field(description="Name of the new table")],
    columns_definition: Annotated[str, Field(description="Column definitions in SQL format, e.g. 'ID INTEGER PRIMARY KEY, NAME NVARCHAR(100), AMOUNT DECIMAL(15,2)'")],
    schema_name: Annotated[str | None, Field(description="Target schema. Defaults to current schema.")] = None,
    table_type: Annotated[str, Field(description="Table type: 'COLUMN' or 'ROW' (default: COLUMN)")] = "COLUMN",
) -> str:
    """Create a new table in the SAP HANA database."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    try:
        ddl = f'CREATE {table_type} TABLE "{schema}"."{table_name}" ({columns_definition})'
        cursor.execute(ddl)
        conn.commit()
        cursor.close()
        return f"Table `{schema}.{table_name}` created successfully as {table_type} store.\n\n```sql\n{ddl}\n```"
    except Exception as exc:
        cursor.close()
        return f"Error creating table: {exc}"


@mcp.tool()
async def drop_table(
    table_name: Annotated[str, Field(description="Name of the table to drop")],
    schema_name: Annotated[str | None, Field(description="Schema name. Defaults to current schema.")] = None,
) -> str:
    """Drop (delete) a table from the SAP HANA database. USE WITH CAUTION."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    try:
        cursor.execute(f'DROP TABLE "{schema}"."{table_name}"')
        conn.commit()
        cursor.close()
        return f"Table `{schema}.{table_name}` dropped successfully."
    except Exception as exc:
        cursor.close()
        return f"Error dropping table: {exc}"


@mcp.tool()
async def create_view(
    view_name: Annotated[str, Field(description="Name of the view to create")],
    select_query: Annotated[str, Field(description="The SELECT statement that defines the view")],
    schema_name: Annotated[str | None, Field(description="Target schema. Defaults to current schema.")] = None,
) -> str:
    """Create a new SQL view in the database."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    try:
        ddl = f'CREATE VIEW "{schema}"."{view_name}" AS {select_query}'
        cursor.execute(ddl)
        conn.commit()
        cursor.close()
        return f"View `{schema}.{view_name}` created successfully."
    except Exception as exc:
        cursor.close()
        return f"Error creating view: {exc}"


@mcp.tool()
async def create_procedure(
    procedure_name: Annotated[str, Field(description="Name of the stored procedure")],
    procedure_body: Annotated[str, Field(description="Full procedure body in SQLScript, including parameters and BEGIN/END block")],
    schema_name: Annotated[str | None, Field(description="Target schema. Defaults to current schema.")] = None,
) -> str:
    """Create a new stored procedure in the SAP HANA database."""
    conn = _get_connection()
    schema = schema_name or _current_schema
    cursor = conn.cursor()
    try:
        ddl = f'CREATE PROCEDURE "{schema}"."{procedure_name}" {procedure_body}'
        cursor.execute(ddl)
        conn.commit()
        cursor.close()
        return f"Procedure `{schema}.{procedure_name}` created successfully."
    except Exception as exc:
        cursor.close()
        return f"Error creating procedure: {exc}"


# -------------------------------------------------------------------
# 9. Custom Query (AI-driven)
# -------------------------------------------------------------------

@mcp.tool()
async def execute_custom_query(
    query: Annotated[str, Field(description="The SQL query to execute on the SAP HANA database. Can be SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, or any valid SQL statement.")],
) -> str:
    """
    Execute a custom SQL query on the connected SAP HANA database.
    
    Use this tool when the user asks a question that can be answered by
    querying the database. Frame the appropriate SQL query based on the
    user's request and execute it.
    
    For SELECT queries, results are returned as a formatted table.
    For DML/DDL queries, the affected row count or success status is returned.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(query)
        # Check if this is a SELECT-like statement that returns rows
        if cursor.description:
            cols = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            cursor.close()
            return (
                f"**Query:**\n```sql\n{query}\n```\n\n"
                f"**Results ({len(rows)} rows):**\n\n" + _rows_to_table(cols, rows)
            )
        else:
            # DML / DDL
            rowcount = cursor.rowcount
            conn.commit()
            cursor.close()
            return (
                f"**Query:**\n```sql\n{query}\n```\n\n"
                f"Statement executed successfully. Rows affected: {rowcount}"
            )
    except Exception as exc:
        cursor.close()
        return f"**Query:**\n```sql\n{query}\n```\n\n**Error:** {exc}"


# ===================================================================
# SSE TRANSPORT & STARLETTE APP
# ===================================================================

sse_transport = SseServerTransport("/messages/")

async def handle_sse(request: Request):
    """SSE endpoint – MCP clients connect here to receive server events."""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp._mcp_server.run(
            streams[0], streams[1], mcp._mcp_server.create_initialization_options()
        )

async def handle_messages(request: Request):
    """POST endpoint – MCP clients send JSON-RPC messages here."""
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)

app = Starlette(
    debug=True,
    routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/messages/", endpoint=handle_messages, methods=["POST"]),
    ],
)


# ===================================================================
# MAIN ENTRY POINT
# ===================================================================

def main():
    """Load system configs and start the SSE server."""
    _load_systems_from_env()
    logger.info("Loaded %d HANA system(s): %s", len(_systems), list(_systems.keys()))
    logger.info("Starting SAP HANA MCP Server on http://0.0.0.0:8000")
    logger.info("SSE endpoint:      http://0.0.0.0:8000/sse")
    logger.info("Messages endpoint: http://0.0.0.0:8000/messages/")
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
