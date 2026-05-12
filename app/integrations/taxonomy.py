import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
TAXONOMY_FILE = DATA_DIR / "etsy_taxonomy.json"
FLAT_TAXONOMY_FILE = DATA_DIR / "etsy_taxonomy_flat.json"

def flatten_taxonomy():
    if not TAXONOMY_FILE.exists():
        return None
    
    with open(TAXONOMY_FILE, "r") as f:
        data = json.load(f)
    
    flat = {}
    
    def walk(nodes, path=""):
        for node in nodes:
            name = node["name"]
            node_id = str(node["id"])
            full_name = f"{path} > {name}" if path else name
            flat[node_id] = full_name
            if "children" in node and node["children"]:
                walk(node["children"], full_name)
    
    walk(data.get("results", []))
    
    with open(FLAT_TAXONOMY_FILE, "w") as f:
        json.dump(flat, f, indent=2)
    
    return flat

def get_taxonomy_name(taxonomy_id):
    if not FLAT_TAXONOMY_FILE.exists():
        flatten_taxonomy()
    
    if not FLAT_TAXONOMY_FILE.exists():
        return f"Unknown ({taxonomy_id})"
    
    with open(FLAT_TAXONOMY_FILE, "r") as f:
        flat = json.load(f)
    
    return flat.get(str(taxonomy_id), f"Unknown ({taxonomy_id})")

def search_taxonomy(query):
    if not FLAT_TAXONOMY_FILE.exists():
        flatten_taxonomy()
    
    if not FLAT_TAXONOMY_FILE.exists():
        return []
    
    with open(FLAT_TAXONOMY_FILE, "r") as f:
        flat = json.load(f)
    
    query = query.lower()
    results = []
    for node_id, name in flat.items():
        if query in name.lower():
            results.append({"id": node_id, "name": name})
            if len(results) > 50: # Limit results
                break
    
    return results

if __name__ == "__main__":
    flatten_taxonomy()
    print(f"Flattened {len(json.load(open(FLAT_TAXONOMY_FILE)))} nodes.")
