import requests
import logging
import argparse
import os
import sys
import traceback
import yaml


DEVICE_ROLES = ["Conference+Router", "SCN+Router", "SCN+Switch", "Conference+Switch", "DNOC+Switch", "Wireless+Switch", "Management+Edge+Switch", "Management+Distribution+Switch"]
DEVICE_STATUS = "Active"


OS_PLATFORM_MAP = {
    "ios": "IOS",
    "ios-xe": "IOS-XE",
    "ios-xr": "IOS-XR",
    "nxos": "NX-OS",
    "eos": "EOS",
    "junos": "JunOS",
    "sros": "SROS",
    "vrp": "VRP",
}


class DeviceMetadataCLI:
    def __init__(self, api_url, api_token, network=None):
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.network = network
        self.logger = logging.getLogger(__name__)
        self.headers = {
            "Authorization": f"Token {self.api_token}",
            "Content-Type": "application/json",
        }

    def _get(self, path):
        url = f"{self.api_url}{path}"
        results = []
        while url:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            results.extend(data.get("results", []))
            url = data.get("next")
        return results

    # Description suffixes (after "<device-name> ") that identify the primary loopback.
    # Juniper: lo0.0 (IPv6) + lo0.999 (IPv4); Nokia: system0 / system.
    _PRIMARY_LOOPBACK_SUFFIXES = {"lo0.0", "lo0.999", "system0", "system"}

    def _resolve_loopback_ips(self, device_name):
        """Return [ipv4, ipv6] primary loopback IPs for the device.

        Uses ?device=<name>&role=Loopback and picks addresses whose description
        ends with a known primary-loopback interface name suffix.
        """
        all_loopbacks = self._get(f"/api/ipam/ip-addresses/?device={device_name}&role=Loopback")
        ips = []
        for ip in all_loopbacks:
            desc = ip.get("description", "") or ""
            # Description format: "<device-name> <iface>" — extract the iface part
            iface_part = desc.removeprefix(device_name).strip()
            if iface_part in self._PRIMARY_LOOPBACK_SUFFIXES:
                addr = ip.get("address", "").split("/")[0]
                if addr:
                    ips.append(addr)
        return ips

    def _management_ips_from_device(self, device):
        """Extract management IPs from primary_ip4/primary_ip6 (already embedded at depth=1)."""
        ips = []
        for field in ("primary_ip4", "primary_ip6"):
            obj = device.get(field) or {}
            addr = (obj.get("host") or obj.get("address", "").split("/")[0])
            if addr:
                ips.append(addr)
        return ips or None

    def _resolve_location(self, location_obj):
        """Walk the location hierarchy to extract city, region, country, and coordinates."""
        info = {
            "location_name": None,
            "location_type": [],
            "city_name": None,
            "continent_name": None,
            "country_name": None,
            "country_code": None,
            "country_sub_name": None,
            "country_sub_code": None,
            "latitude": None,
            "longitude": None,
        }
        if not location_obj:
            return info

        info["location_name"] = location_obj.get("name")

        loc_type = (location_obj.get("location_type") or {}).get("name", "")
        if loc_type:
            info["location_type"] = [loc_type.lower().replace(" ", "_")]

        # Latitude/longitude may be custom fields
        cf = location_obj.get("custom_fields", {}) or {}
        info["latitude"] = cf.get("latitude") or location_obj.get("latitude")
        info["longitude"] = cf.get("longitude") or location_obj.get("longitude")

        # Walk parent hierarchy to find city/region/country
        # Nautobot returns a nested "parent" key within the location detail
        loc_detail_url = location_obj.get("url")
        if loc_detail_url:
            try:
                detail = requests.get(loc_detail_url, headers=self.headers).json()
                parent = detail.get("parent")
                depth = 0
                # Walk up max 5 levels
                while parent and depth < 5:
                    parent_detail = requests.get(parent["url"], headers=self.headers).json()
                    ptype = (parent_detail.get("location_type") or {}).get("name", "").lower()
                    pname = parent_detail.get("name", "")
                    if ptype in ("city",) and not info["city_name"]:
                        info["city_name"] = pname
                    if ptype in ("region", "state", "province") and not info["country_sub_name"]:
                        info["country_sub_name"] = pname
                        info["country_sub_code"] = parent_detail.get("slug", "").upper() or None
                    if ptype in ("country",) and not info["country_name"]:
                        info["country_name"] = pname
                        info["country_code"] = parent_detail.get("slug", "").upper() or None
                    if ptype in ("continent",) and not info["continent_name"]:
                        info["continent_name"] = pname
                    parent = parent_detail.get("parent")
                    depth += 1
            except Exception:
                pass

        return info

    def fetch_metadata(self):
        role_query = "&".join(f"role={r}" for r in DEVICE_ROLES)
        devices = self._get(f"/api/dcim/devices/?{role_query}&status={DEVICE_STATUS}&depth=1")

        entries = []
        for device in devices:
            device_id = device["id"]
            name = device.get("name", "")

            # Platform / OS
            platform = device.get("platform") or {}
            platform_slug = (platform.get("slug") or "").lower()
            os_name = OS_PLATFORM_MAP.get(platform_slug) or platform.get("name") or None

            # Manufacturer / model
            device_type = device.get("device_type") or {}
            model = device_type.get("model") or None
            manufacturer_obj = device_type.get("manufacturer") or {}
            manufacturer = manufacturer_obj.get("name") or None

            # Role: use the display name directly from Nautobot
            role_obj = device.get("role") or {}
            role_label = role_obj.get("display") or role_obj.get("name") or None

            # Management IPs from primary_ip4/primary_ip6 (embedded at depth=1)
            management_ips = self._management_ips_from_device(device)

            # Location hierarchy (embedded at depth=1)
            location_obj = device.get("location") or {}
            loc_info = self._resolve_location(location_obj)

            # Hostname from management IP dns_name (primary_ip4 preferred)
            hostname = None
            for field in ("primary_ip4", "primary_ip6"):
                dns = (device.get(field) or {}).get("dns_name")
                if dns:
                    hostname = dns
                    break

            entry = {
                "id": name,
                "type": "router",
                "loopback_ip": management_ips,
                "management_ip": management_ips,
                "hostname": hostname,
                "location_name": loc_info["location_name"],
                "location_type": loc_info["location_type"] or None,
                "city_name": loc_info["city_name"],
                "continent_name": loc_info["continent_name"],
                "country_name": loc_info["country_name"],
                "country_code": loc_info["country_code"],
                "country_sub_name": loc_info["country_sub_name"],
                "country_sub_code": loc_info["country_sub_code"],
                "latitude": loc_info["latitude"],
                "longitude": loc_info["longitude"],
                "manufacturer": manufacturer,
                "model": model,
                "network": self.network or None,
                "os": os_name,
                "role": role_label,
                "state": "active",
            }
            # Drop keys whose value is None to keep the YAML tidy
            entry = {k: v for k, v in entry.items() if v is not None}
            entries.append(entry)

        return entries

    def to_yaml(self, entries):
        doc = {"table": "meta_device", "data": entries}

        class NoAliasDumper(yaml.Dumper):
            def ignore_aliases(self, data):
                return True

        return yaml.dump(doc, Dumper=NoAliasDumper, default_flow_style=False, allow_unicode=True, sort_keys=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch Core/SCN router metadata from Nautobot and emit meta_device YAML"
    )
    parser.add_argument("--url", help="Nautobot base URL (or set URL env var)")
    parser.add_argument("--api-token", help="Nautobot API token (or set API_TOKEN env var)")
    parser.add_argument("--network", help="Network name to embed in output (or set NETWORK env var)")
    parser.add_argument("--output-file", help="Write YAML output to this file (default: stdout)")
    args = parser.parse_args()

    api_url = args.url or os.environ.get("URL")
    api_token = args.api_token or os.environ.get("API_TOKEN")
    network = args.network or os.environ.get("NETWORK")
    output_file = args.output_file or os.environ.get("DEVICE_OUTPUT_FILE")

    if not api_url:
        print("Error: --url is required (or set URL environment variable)", file=sys.stderr)
        sys.exit(1)
    if not api_token:
        print("Error: --api-token is required (or set API_TOKEN environment variable)", file=sys.stderr)
        sys.exit(1)

    cli = DeviceMetadataCLI(api_url, api_token, network=network)

    try:
        entries = cli.fetch_metadata()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    yaml_output = cli.to_yaml(entries)

    if output_file:
        with open(output_file, "w") as f:
            f.write(yaml_output)
        print(f"Metadata written to {output_file}")
    else:
        print(yaml_output)
