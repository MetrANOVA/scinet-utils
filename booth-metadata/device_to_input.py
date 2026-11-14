#!/usr/bin/env python3
"""Generate per-host Telegraf SNMP input configs from a device metadata YAML file.

Usage:
    python device_to_input.py -y metadata_device.yml -t 10_input_snmp_HOSTNAME.conf.example -o ./output/

All options can also be set via environment variables (CLI takes precedence):
    DEVICE_YAML         Path to device metadata YAML
    SNMP_TEMPLATE       Path to the template SNMP input .conf file
    OUTPUT_DIR          Directory to write generated configs into
    FILENAME_PATTERN    Output filename pattern; {hostname} is replaced with
                        the device hostname (default: 10_input_snmp_{hostname}.conf)
    SNMP_PORT           SNMP port to use in agents URL (default: 161)
    SNMP_PROTO          Transport protocol for agents URL (default: udp)
    SNMP_COMMUNITY      SNMP community string to substitute in generated configs
                        (default: no substitution — leave template value as-is)
    FILTER_STATE        Only include devices with this state value (default: active)
                        Set to empty string to disable filtering.
    FORCE               Overwrite existing files (default: false)
"""

import argparse
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Missing dependency: pyyaml. Install with: pip install pyyaml")
    sys.exit(1)


AGENTS_RE = re.compile(
    r'^(\s*agents\s*=\s*\[)[^\]]*\]',
    re.MULTILINE | re.DOTALL,
)
COMMUNITY_RE = re.compile(
    r'^(\s*community\s*=\s*)"[^"]*"',
    re.MULTILINE,
)


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate per-host Telegraf SNMP input configs from device metadata YAML.",
    )
    parser.add_argument("-y", "--yaml", default=env("DEVICE_YAML"),
                        help="Path to device metadata YAML file [DEVICE_YAML]")
    parser.add_argument("-t", "--template", default=env("SNMP_TEMPLATE"),
                        help="Path to template SNMP input .conf file [SNMP_TEMPLATE]")
    parser.add_argument("-o", "--output-dir", default=env("OUTPUT_DIR"),
                        help="Directory to write generated configs into [OUTPUT_DIR]")
    parser.add_argument("--filename-pattern",
                        default=env("FILENAME_PATTERN", "10_input_snmp_{hostname}.conf"),
                        help="Output filename pattern; {hostname} replaced with device hostname "
                             "[FILENAME_PATTERN] (default: 10_input_snmp_{hostname}.conf)")
    parser.add_argument("--port", default=env("SNMP_PORT", "161"),
                        help="SNMP port [SNMP_PORT] (default: 161)")
    parser.add_argument("--proto", default=env("SNMP_PROTO", "udp"),
                        help="Transport protocol [SNMP_PROTO] (default: udp)")
    parser.add_argument("--community", default=env("SNMP_COMMUNITY", ""),
                        help="SNMP community string to substitute into generated configs "
                             "[SNMP_COMMUNITY] (default: leave template value unchanged)")
    parser.add_argument("--filter-state", default=env("FILTER_STATE", "active"),
                        help="Only include devices with this state; empty string disables "
                             "[FILTER_STATE] (default: active)")
    parser.add_argument("--force", action="store_true",
                        default=env("FORCE", "").lower() in {"1", "true", "yes"},
                        help="Overwrite existing output files [FORCE]")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written without writing any files")
    return parser.parse_args()


def load_devices(yaml_path: Path, filter_state: str) -> list[dict]:
    data = yaml.safe_load(yaml_path.read_text())
    if not isinstance(data, dict) or "data" not in data:
        raise ValueError(f"{yaml_path}: expected a YAML object with a 'data' key")
    devices = data["data"]
    if not isinstance(devices, list):
        raise ValueError(f"{yaml_path}: 'data' must be a list")
    if filter_state:
        devices = [d for d in devices if d.get("state") == filter_state]
    return devices


def build_agents_line(hostname: str, proto: str, port: str) -> str:
    return f'agents = [\n"{proto}://{hostname}:{port}"\n]'


def render(template: str, hostname: str, proto: str, port: str, community: str = "") -> str:
    result = AGENTS_RE.sub(
        lambda m: build_agents_line(hostname, proto, port),
        template,
    )
    if community:
        result = COMMUNITY_RE.sub(lambda m: f'{m.group(1)}"{community}"', result)
    return result


def main() -> None:
    args = parse_args()

    missing = [name for name, val in [
        ("--yaml / DEVICE_YAML", args.yaml),
        ("--template / SNMP_TEMPLATE", args.template),
        ("--output-dir / OUTPUT_DIR", args.output_dir),
    ] if not val]
    if missing:
        print("Error: missing required arguments:", ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    yaml_path = Path(args.yaml)
    template_path = Path(args.template)
    output_dir = Path(args.output_dir)

    if not yaml_path.exists():
        print(f"Error: YAML file not found: {yaml_path}", file=sys.stderr)
        sys.exit(1)
    if not template_path.exists():
        print(f"Error: template file not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    template = template_path.read_text()
    if not AGENTS_RE.search(template):
        print("Error: template does not contain an 'agents = [...]' line", file=sys.stderr)
        sys.exit(1)

    devices = load_devices(yaml_path, args.filter_state)
    if not devices:
        print("Warning: no devices found (check --filter-state)", file=sys.stderr)
        sys.exit(0)

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    skipped = written = 0
    for device in devices:
        hostname = device.get("hostname")
        if not hostname:
            print(f"Warning: skipping device with no hostname: {device.get('id', '?')}")
            continue

        filename = args.filename_pattern.format(hostname=hostname)
        output_path = output_dir / filename
        content = render(template, hostname, args.proto, args.port, args.community)

        if args.dry_run:
            print(f"[dry-run] Would write {output_path}")
            continue

        if output_path.exists() and not args.force:
            print(f"Skipped {output_path} (exists, use --force to overwrite)")
            skipped += 1
            continue

        output_path.write_text(content)
        print(f"Wrote {output_path}")
        written += 1

    if not args.dry_run:
        print(f"\nDone: {written} written, {skipped} skipped.")


if __name__ == "__main__":
    main()
