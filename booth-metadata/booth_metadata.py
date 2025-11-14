import json
import csv
import requests
import logging
import argparse
import os
import sys
from collections import defaultdict

class BoothMetadataCLI:
    def __init__(self, api_url, api_token, coords_file):
        self.api_url = api_url
        self.api_token = api_token
        self.coords_file = coords_file
        self.logger = logging.getLogger(__name__)
        self.headers = {
            "Authorization": f"Token {self.api_token}",
            "Content-Type": "application/json"
        }
    def _fetch_url_with_pagination(self, url, headers=None):
        if headers is None:
            headers = self.headers
        results = []
        while url:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            results.extend(data.get("results", []))
            url = data.get("next")
        return results

    def fetch_metadata(self):
        # import csv of coordinates and create dict
        coords_dict = {}
        with open(self.coords_file) as csvfile: 
            coords = csv.reader(csvfile)
            for row in coords:
                coords_dict[row[0]] = {'x': row[1], 'y': row[2]}
        #first fetch the list of prefixes where tenant group is Exhibitor
        prefixes = self._fetch_url_with_pagination(f"{self.api_url}/api/ipam/prefixes/?tenant_group=Exhibitor")
        #then fetch the list of tenants where tenant group is Exhibitor
        tenants = self._fetch_url_with_pagination(f"{self.api_url}/api/tenancy/tenants/?tenant_group=Exhibitor")
        # now get the list of locations
        locations = self._fetch_url_with_pagination(f"{self.api_url}/api/dcim/locations/?tenant_group=Exhibitor")
        # build a map of tenants to tenant name
        tenant_map = {tenant["id"]: tenant["name"] for tenant in tenants}
        # build a map of location tenant ids to location name
        location_map = {location["tenant"]["id"]: location["name"] for location in locations if location.get("tenant", {}).get("id", None) is not None}
        # build temp dict to join prefixes
        temp_dict = defaultdict(lambda: {"addresses": [], "org_name": None, "resource_name": None, "latitude": None, "longitude": None})
        for prefix in prefixes:
            tenant_id = prefix.get("tenant", {}).get("id", None)
            if tenant_id is None or prefix.get("prefix", None) is None:
                continue
            temp_dict[tenant_id]["addresses"].append(prefix["prefix"])
            temp_dict[tenant_id]["org_name"] = tenant_map.get(tenant_id, None)
            temp_dict[tenant_id]["resource_name"] = location_map.get(tenant_id, None)
            booth_num = temp_dict[tenant_id]["resource_name"].split(" ")[1]
            temp_dict[tenant_id]["latitude"] = coords_dict[booth_num]['x']
            temp_dict[tenant_id]["longitude"] = coords_dict[booth_num]['y']

        # now load the intranet info if we have it
        self.fetch_intranet_metadata(temp_dict, coords_dict)

        return list(temp_dict.values())
    
    def fetch_intranet_metadata(self, temp_dict, coords_dict):
        if not self.intranet_api_token or not self.intranet_url:
            return
        intranet_headers = {
            "Authorization": f"Token {self.intranet_api_token}",
            "Content-Type": "application/json"
        }
        # Booths: https://scinet.supercomputing.org/intranet_api/v1/booth/
        # Organization: https://scinet.supercomputing.org/intranet_api/v1/exhibitor_organization/
        # Network: https://scinet.supercomputing.org/intranet_api/v1/network/
        # Networked Connection: https://scinet.supercomputing.org/intranet_api/v1/networked_connection/
        booths = self._fetch_url_with_pagination(f"{self.intranet_url}/booth/", headers=intranet_headers)
        #Build map of booth id to booth object
        booth_map = {booth["id"]: booth for booth in booths}
        # Now fetch the list of organizations
        organizations = self._fetch_url_with_pagination(f"{self.intranet_url}/exhibitor_organization/", headers=intranet_headers)
        org_map = {org["id"]: org.get("name", None) for org in organizations}
        # get list of networks
        networks = self._fetch_url_with_pagination(f"{self.intranet_url}/network/", headers=intranet_headers)
        network_map = {network["id"]: network for network in networks}
        #no get list of connections and we'll add stuff to temp_dict
        connections = self._fetch_url_with_pagination(f"{self.intranet_url}/networked_connection/", headers=intranet_headers)
        for connection in connections:
            booth_id = connection.get("booth", None)
            if booth_id is None:
                continue
            network = network_map.get(connection.get("network", None), {})
            if not network or not (network.get("net", None) or network.get("v6net", None)):
                continue
            booth_name = booth_map.get(booth_id, {}).get("name", None)
            if booth_name is None:
                continue
            org_name = org_map.get(booth_map.get(booth_id, {}).get("organization", None), None)
            if org_name is None:
                org_name = "unknown"
            booth_key = f"booth_{booth_id}"
            if network.get("net", None):
                temp_dict[booth_key]["addresses"].append(network["net"])
            if network.get("v6net", None):
                temp_dict[booth_key]["addresses"].append(network["v6net"])
            temp_dict[booth_key]["org_name"] = org_name
            temp_dict[booth_key]["resource_name"] = booth_name
            booth_num = temp_dict[booth_key]["resource_name"].split(" ")[1]
            if booth_num in coords_dict:
                temp_dict[booth_key]["latitude"] = coords_dict[booth_num]['x']
                temp_dict[booth_key]["longitude"] = coords_dict[booth_num]['y']

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch booth metadata from Nautobot API")
    parser.add_argument("--url", help="Nautobot API URL")
    parser.add_argument("--api-token", help="Nautobot API token")
    parser.add_argument("--intranet-api-token", help="Intranet API token")
    parser.add_argument("--intranet-url", help="Intranet API URL")
    parser.add_argument("--coords-file", help="Coordinates CSV file path")
    parser.add_argument("--output-file", help="Output file path")
    args = parser.parse_args()
    
    # Get values from args or environment variables
    api_url = args.url or os.environ.get("URL")
    api_token = args.api_token or os.environ.get("API_TOKEN")
    coords_file = args.coords_file or os.environ.get("COORDS_FILE")
    output_file = args.output_file or os.environ.get("OUTPUT_FILE")

    # Validate required parameters
    if not api_url:
        print("Error: --url is required (or set URL environment variable)", file=sys.stderr)
        sys.exit(1)
    
    if not api_token:
        print("Error: --api-token is required (or set API_TOKEN environment variable)", file=sys.stderr)
        sys.exit(1)

    if not coords_file:
        print("Error: --coords-file is required (or set COORDS_FILE environment variable)", file=sys.stderr)
        sys.exit(1)
    
    # Initialize CLI
    cli = BoothMetadataCLI(api_url, api_token, coords_file)
    # Lookup Intranet info if we have it
    if args.intranet_api_token and args.intranet_url:
        cli.intranet_api_token = args.intranet_api_token
        cli.intranet_url = args.intranet_url
    elif os.environ.get("INTRANET_API_TOKEN") and os.environ.get("INTRANET_URL"):
        cli.intranet_api_token = os.environ.get("INTRANET_API_TOKEN")
        cli.intranet_url = os.environ.get("INTRANET_URL")
    # Fetch metadata from nautobot and return json
    metadata_json = None
    try:
        metadata_json = cli.fetch_metadata()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1) 
    if metadata_json is None:
        print("Error: Failed to fetch metadata", file=sys.stderr)
        sys.exit(1)

    #If output file is specified, write to file, otherwise print to stdout
    if output_file:
        with open(output_file, "w") as f:
            f.write(json.dumps(metadata_json))
        print(f"Metadata written to {output_file}")
    else:
        print(json.dumps(metadata_json, indent=2))