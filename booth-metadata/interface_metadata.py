import requests
import logging
import argparse
import os
import sys
import re
import traceback
import yaml
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PAGE_SIZE = 1000
MAX_WORKERS = 10


DEVICE_ROLES = ["Conference+Router", "SCN+Router", "SCN+Switch", "Conference+Switch", "DNOC+Switch", "Wireless+Switch", "Management+Edge+Switch", "Management+Distribution+Switch"]
DEVICE_STATUS = "Active"

# Interface roles that represent external peerings (edge: true)
EDGE_ROLES = {
    "WAN Peering (L3)",
    "WAN Transit (L2)",
    "NRE Connection (L3)",
    "NRE Connection (L2)",
    "NRE Connection (L2 Multipoint)",
    "Exhibitor Connection (L3)",
}

# Interface roles that are backbone/internal (edge: false)
BACKBONE_ROLES = {
    "Backbone",
    "Backbone Downlink (L2)",
    "Point-to-Point",
    "DWDM",
    "Optical",
    "Peerlink",
    "Loopback",
    "Management",
}


def _iface_type(iface):
    """Map Nautobot interface type/role to YAML type string."""
    nautobot_type = (iface.get("type") or {}).get("value", "")
    if nautobot_type == "lag":
        return "lag"
    if nautobot_type == "virtual":
        return "service_l3vpn"
    return "port"


def _is_edge(iface):
    """Return True if interface is an external peering (edge)."""
    role_name = (iface.get("role") or {}).get("display", "")
    if role_name in EDGE_ROLES:
        return True
    if role_name in BACKBONE_ROLES:
        return False
    # Default to False for unknown roles
    return False


def _extract_iface_from_ip_description(device_name, desc):
    """Extract interface name from an IP description string.

    Two formats:
      1. "<device>:<iface>: IPv4/6 Connection: ..."  → extract <iface>
      2. "<device> <iface>"                           → extract <iface>
    """
    if not desc:
        return None
    # Format 1: "Device: <device>:<iface>: IPv4/6 Connection: ..."
    m = re.match(r"Device:\s*[^:]+:([^:]+):", desc)
    if m:
        return m.group(1).strip()
    # Format 2: "<device> <iface>"
    if desc.startswith(device_name + " "):
        return desc[len(device_name) + 1:].strip()
    return None


def _ch_query(ch_url, ch_username, ch_password, ch_database, query):
    """Run a ClickHouse query and return parsed JSONEachRow rows."""
    import json
    response = requests.post(
        ch_url,
        params={"database": ch_database},
        data=query.encode(),
        auth=(ch_username, ch_password),
        headers={"Content-Type": "text/plain"},
        timeout=60,
        verify=False,
    )
    response.raise_for_status()
    rows = []
    for line in response.text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def fetch_ifalias_map(ch_url, ch_username, ch_password, ch_database, lookback_hours=24):
    """Query ClickHouse for ifAlias and return a (device_id, if_index) → if_alias dict.

    Keyed by (device_id, oid_index) so it can be joined with the ifName map.
    """
    now = datetime.now(timezone.utc)
    time_from = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d %H:%M:%S")
    time_to = now.strftime("%Y-%m-%d %H:%M:%S")

    query = f"""
SELECT
    id,
    argMax(ext.oidIndex, observation_time) AS if_index,
    argMax(ext.metric_value, observation_time) AS if_alias
FROM data_snmp_counter
WHERE metric_name = 'SNMP_IF-MIB::ifAlias'
  AND observation_time >= '{time_from}'
  AND observation_time <= '{time_to}'
GROUP BY id, ext.oidIndex
SETTINGS allow_suspicious_types_in_group_by = 1
FORMAT JSONEachRow
"""
    descr_map = {}
    for row in _ch_query(ch_url, ch_username, ch_password, ch_database, query):
        device_id = row.get("id", "")
        if_index = row.get("if_index")
        if_alias = row.get("if_alias") or None
        if device_id and if_index is not None:
            descr_map[(device_id, if_index)] = if_alias
    return descr_map


def fetch_flow_index_map(ch_url, ch_username, ch_password, ch_database, lookback_hours=24):
    """Query ClickHouse for ifName SNMP data and return a (device_id, if_name) → if_index dict."""
    now = datetime.now(timezone.utc)
    time_from = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d %H:%M:%S")
    time_to = now.strftime("%Y-%m-%d %H:%M:%S")

    query = f"""
SELECT
    id,
    argMax(ext.metric_value, observation_time) AS if_name,
    argMax(ext.oidIndex, observation_time) AS if_index,
    max(observation_time) AS update_time
FROM data_snmp_counter
WHERE metric_name = 'SNMP_IF-MIB::ifName'
  AND observation_time >= '{time_from}'
  AND observation_time <= '{time_to}'
GROUP BY id, ext.metric_value, ext.oidIndex
ORDER BY id, if_name, if_index
SETTINGS allow_suspicious_types_in_order_by = 1, allow_suspicious_types_in_group_by = 1
FORMAT JSONEachRow
"""

    rows = _ch_query(ch_url, ch_username, ch_password, ch_database, query)
    flow_index_map = {}
    for row in rows:
        device_id = row.get("id", "")
        if_name = row.get("if_name", "")
        if_index = row.get("if_index")
        if device_id and if_name and if_index is not None:
            flow_index_map[(device_id, if_name)] = if_index
    return flow_index_map


class InterfaceMetadataCLI:
    def __init__(self, api_url, api_token):
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.logger = logging.getLogger(__name__)
        self.headers = {
            "Authorization": f"Token {self.api_token}",
            "Content-Type": "application/json",
        }

    def _get(self, path):
        sep = "&" if "?" in path else "?"
        url = f"{self.api_url}{path}{sep}limit={PAGE_SIZE}"
        results = []
        while url:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            results.extend(data.get("results", []))
            url = data.get("next")
        return results

    def _fetch_devices(self):
        role_query = "&".join(f"role={r}" for r in DEVICE_ROLES)
        return self._get(f"/api/dcim/devices/?{role_query}&status={DEVICE_STATUS}&depth=1")

    def _fetch_interfaces_for_device(self, device_name):
        return device_name, self._get(f"/api/dcim/interfaces/?device={device_name}&depth=1")

    def _fetch_ips_for_device(self, device_name):
        """Fetch all IPs for a device and return a dict keyed by interface name."""
        all_ips = self._get(f"/api/ipam/ip-addresses/?device={device_name}")
        iface_ips = {}
        for ip in all_ips:
            desc = ip.get("description", "") or ""
            iface_name = _extract_iface_from_ip_description(device_name, desc)
            if iface_name:
                iface_ips.setdefault(iface_name, []).append(ip.get("address", ""))
        return device_name, iface_ips

    # Cache for remote interface lookups: uuid → "device::iface" (or None)
    _remote_iface_cache: dict = {}

    def _resolve_remote_interface_id(self, ce, iface_uuid_map):
        """Given a connected_endpoint object, return 'device::iface' string.

        Uses the local uuid map first. If the remote interface UUID is not in
        the map (i.e., the remote device is not one of our target devices),
        fetch the remote interface detail to get device name and interface name.
        Results are cached so the same remote interface is only fetched once.
        """
        if not ce:
            return None
        ce_id = ce.get("id")
        if not ce_id:
            return None

        # Try local map first (remote is one of our target devices)
        if ce_id in iface_uuid_map:
            return iface_uuid_map[ce_id]

        # Check cache
        if ce_id in self._remote_iface_cache:
            return self._remote_iface_cache[ce_id]

        # depth=1 returns device inline, avoiding a second request
        result = None
        try:
            resp = requests.get(
                f"{self.api_url}/api/dcim/interfaces/{ce_id}/?depth=1",
                headers=self.headers,
            )
            resp.raise_for_status()
            remote = resp.json()
            remote_dev_name = (remote.get("device") or {}).get("name", "")
            remote_iface_name = remote.get("name", "")
            if remote_dev_name and remote_iface_name:
                result = f"{remote_dev_name}::{remote_iface_name}"
        except Exception:
            pass

        self._remote_iface_cache[ce_id] = result
        return result

    def fetch_metadata(self, flow_index_map=None, ifalias_map=None):
        devices = self._fetch_devices()
        device_names = [d["name"] for d in devices]

        # Build device_name → hostname map (hostname = dns_name from primary_ip4/6)
        device_hostname_map = {}
        for d in devices:
            hostname = None
            for field in ("primary_ip4", "primary_ip6"):
                dns = (d.get(field) or {}).get("dns_name")
                if dns:
                    hostname = dns
                    break
            if hostname:
                device_hostname_map[d["name"]] = hostname

        # Phase 1: fetch all interfaces in parallel, build uuid→id map
        all_ifaces_by_device = {}  # device_name → list of interface dicts
        iface_uuid_map = {}        # uuid → "device_name::iface_name"

        self.logger.info("Fetching interfaces for %d devices in parallel", len(device_names))
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(self._fetch_interfaces_for_device, dn): dn for dn in device_names}
            for future in as_completed(futures):
                device_name, ifaces = future.result()
                all_ifaces_by_device[device_name] = ifaces
                for iface in ifaces:
                    uid = iface.get("id")
                    if uid:
                        iface_uuid_map[uid] = f"{device_name}::{iface['name']}"

        # Phase 2: fetch all IPs in parallel, then build entries
        self.logger.info("Fetching IPs for %d devices in parallel", len(device_names))
        all_ips_by_device = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(self._fetch_ips_for_device, dn): dn for dn in device_names}
            for future in as_completed(futures):
                device_name, iface_ips = future.result()
                all_ips_by_device[device_name] = iface_ips

        entries = []
        for device_name in device_names:
            ifaces = all_ifaces_by_device[device_name]
            iface_ips = all_ips_by_device[device_name]

            # Build a name→uuid map for this device's interfaces (for lag member lookup)
            name_to_uuid = {iface["name"]: iface["id"] for iface in ifaces}

            for iface in ifaces:
                iface_id_str = f"{device_name}::{iface['name']}"
                iface_name = iface["name"]
                iface_type = _iface_type(iface)
                nautobot_type_val = (iface.get("type") or {}).get("value", "")
                edge = _is_edge(iface)

                # VRF
                vrf_obj = iface.get("vrf") or {}
                vrtr_name = vrf_obj.get("name") or None
                vrtr_id = None
                if vrf_obj:
                    cf = vrf_obj.get("custom_fields") or {}
                    vrtr_id = cf.get("numeric_id")
                    if vrtr_id is not None:
                        vrtr_id = str(vrtr_id)

                # IPs from description-parsed map
                raw_ips = iface_ips.get(iface_name, [])
                ipv4_list = [a.split("/")[0] for a in raw_ips if ":" not in a]
                ipv6_list = [a.split("/")[0] for a in raw_ips if ":" in a]

                # Speed — None on LAGs; derive from member speeds if available
                speed = iface.get("speed")
                if speed is None and iface_type == "lag":
                    # Count members whose uuid we know and try to get their speed
                    member_speeds = []
                    for candidate in ifaces:
                        lag_ref = candidate.get("lag") or {}
                        if lag_ref.get("id") == iface.get("id"):
                            s = candidate.get("speed")
                            if s:
                                member_speeds.append(s)
                    if member_speeds:
                        # speed in Kbps → Mbps
                        speed = sum(member_speeds) // 1000
                if speed is not None and iface_type != "lag":
                    # Convert Kbps to Mbps
                    speed = speed // 1000

                # remote_interface_id from connected_endpoint
                ce = iface.get("connected_endpoint") or {}
                remote_id = self._resolve_remote_interface_id(ce if ce else None, iface_uuid_map)

                # port_interface_id — for virtual (sub-interfaces), the parent LAG/physical port
                port_interface_id = None
                parent_ref = iface.get("parent_interface") or {}
                parent_uuid = parent_ref.get("id")
                if parent_uuid and parent_uuid in iface_uuid_map:
                    port_interface_id = iface_uuid_map[parent_uuid]
                elif parent_uuid:
                    # parent is an interface on this device (should always be in map)
                    parent_name = next(
                        (i["name"] for i in ifaces if i["id"] == parent_uuid), None
                    )
                    if parent_name:
                        port_interface_id = f"{device_name}::{parent_name}"

                # lag_member_interface_id — for LAG interfaces
                lag_members = None
                if iface_type == "lag":
                    members = [
                        iface_uuid_map[i["id"]]
                        for i in ifaces
                        if (i.get("lag") or {}).get("id") == iface.get("id")
                        and i["id"] in iface_uuid_map
                    ]
                    if members:
                        lag_members = members

                entry = {
                    "id": iface_id_str,
                    "type": iface_type,
                    "description": iface.get("description") or None,
                    "device_id": device_name,
                    "edge": edge,
                    "name": iface_name,
                }

                if vrtr_name or vrtr_id:
                    entry["ext"] = {}
                    if vrtr_id:
                        entry["ext"]["vrtr_id"] = vrtr_id
                    if vrtr_name:
                        entry["ext"]["vrtr_name"] = vrtr_name

                if ipv4_list:
                    entry["ipv4"] = ipv4_list[0] if len(ipv4_list) == 1 else ipv4_list
                if ipv6_list:
                    entry["ipv6"] = ipv6_list[0] if len(ipv6_list) == 1 else ipv6_list

                if speed is not None:
                    entry["speed"] = speed

                if port_interface_id:
                    entry["port_interface_id"] = port_interface_id

                if remote_id:
                    entry["remote_interface_id"] = remote_id

                if lag_members:
                    entry["lag_member_interface_id"] = lag_members

                if flow_index_map:
                    hostname = device_hostname_map.get(device_name)
                    flow_index = flow_index_map.get((hostname, iface_name)) if hostname else None
                    if flow_index is not None:
                        entry["flow_index"] = flow_index

                # Drop None values
                entry = {k: v for k, v in entry.items() if v is not None}
                entries.append(entry)

        # Post-processing: any interface whose remote_interface_id is in our own
        # output set is an internal link — override edge to False regardless of role.
        known_ids = {e["id"] for e in entries}
        for entry in entries:
            remote = entry.get("remote_interface_id")
            if remote and remote in known_ids:
                entry["edge"] = False

        # Append synthetic entries for CH-only interfaces (not in Nautobot)
        if flow_index_map:
            entries = self.add_ch_only_entries(entries, flow_index_map, ifalias_map, device_hostname_map)

        return entries

    def add_ch_only_entries(self, entries, flow_index_map, ifalias_map, device_hostname_map):
        """Append synthetic entries for CH interfaces not present in Nautobot."""
        # Build reverse map: hostname → device_name
        hostname_to_device = {v: k for k, v in device_hostname_map.items()}

        known_ids = {e["id"] for e in entries}
        added = 0
        for (hostname, if_name), if_index in flow_index_map.items():
            device_name = hostname_to_device.get(hostname)
            if not device_name:
                continue
            iface_id_str = f"{device_name}::{if_name}"
            if iface_id_str in known_ids:
                continue

            # Derive port_interface_id for sub-interface names like "ae0.1020" or "et-0/0/0.100"
            port_interface_id = None
            m = re.match(r"^(.+?)[./](\d+)$", if_name)
            if m:
                port_name = m.group(1)
                port_interface_id = f"{device_name}::{port_name}"

            entry = {
                "id": iface_id_str,
                "type": "port",
                "device_id": device_name,
                "edge": False,
                "name": if_name,
                "flow_index": if_index,
            }

            # Join ifAlias by (hostname, if_index) since that's what the descr map is keyed on
            if_alias = ifalias_map.get((hostname, if_index)) if ifalias_map else None
            if if_alias:
                entry["description"] = if_alias

            if port_interface_id:
                entry["port_interface_id"] = port_interface_id

            entries.append(entry)
            known_ids.add(iface_id_str)
            added += 1

        self.logger.info("Added %d synthetic entries from ClickHouse not in Nautobot", added)
        return entries

    def to_yaml(self, entries):
        doc = {"table": "meta_interface", "data": entries}
        return yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch router interface metadata from Nautobot and emit meta_interface YAML"
    )
    parser.add_argument("--url", help="Nautobot base URL (or set URL env var)")
    parser.add_argument("--api-token", help="Nautobot API token (or set API_TOKEN env var)")
    parser.add_argument("--output-file", help="Write YAML output to this file (default: stdout)")
    parser.add_argument("--ch-url", help="ClickHouse HTTP URL (or set CLICKHOUSE_URL env var)")
    parser.add_argument("--ch-username", help="ClickHouse username (or set CLICKHOUSE_USERNAME env var)")
    parser.add_argument("--ch-password", help="ClickHouse password (or set CLICKHOUSE_PASSWORD env var)")
    parser.add_argument("--ch-database", help="ClickHouse database (or set CLICKHOUSE_DATABASE env var)", default="metranova")
    args = parser.parse_args()

    api_url = args.url or os.environ.get("URL")
    api_token = args.api_token or os.environ.get("API_TOKEN")
    output_file = args.output_file or os.environ.get("INTERFACE_OUTPUT_FILE")
    ch_url = args.ch_url or os.environ.get("CLICKHOUSE_URL")
    ch_username = args.ch_username or os.environ.get("CLICKHOUSE_USERNAME")
    ch_password = args.ch_password or os.environ.get("CLICKHOUSE_PASSWORD")
    ch_database = args.ch_database or os.environ.get("CLICKHOUSE_DATABASE", "metranova")

    if not api_url:
        print("Error: --url is required (or set URL environment variable)", file=sys.stderr)
        sys.exit(1)
    if not api_token:
        print("Error: --api-token is required (or set API_TOKEN environment variable)", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    logger = logging.getLogger(__name__)
    cli = InterfaceMetadataCLI(api_url, api_token)

    flow_index_map = None
    ifalias_map = None
    if ch_url and ch_username and ch_password:
        try:
            logger.info("Fetching flow index data from ClickHouse")
            flow_index_map = fetch_flow_index_map(ch_url, ch_username, ch_password, ch_database)
            logger.info("Loaded %d flow index entries from ClickHouse", len(flow_index_map))
        except Exception as e:
            logger.warning("Failed to fetch flow index from ClickHouse: %s", e)
        try:
            logger.info("Fetching ifAlias data from ClickHouse")
            ifalias_map = fetch_ifalias_map(ch_url, ch_username, ch_password, ch_database)
            logger.info("Loaded %d ifAlias entries from ClickHouse", len(ifalias_map))
        except Exception as e:
            logger.warning("Failed to fetch ifAlias from ClickHouse: %s", e)

    try:
        entries = cli.fetch_metadata(flow_index_map=flow_index_map, ifalias_map=ifalias_map)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    yaml_output = cli.to_yaml(entries)

    if output_file:
        with open(output_file, "w") as f:
            f.write(yaml_output)
        print(f"Interface metadata written to {output_file}")
    else:
        print(yaml_output)
