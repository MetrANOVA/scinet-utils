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
    def _fetch_url_with_pagination(self, url):
        results = []
        while url:
            response = requests.get(url, headers=self.headers)
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

        return list(temp_dict.values())

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch booth metadata from Nautobot API")
    parser.add_argument("--url", help="Nautobot API URL")
    parser.add_argument("--api-token", help="Nautobot API token")
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