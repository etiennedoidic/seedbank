from lib.catalog import search_rows
from lib.error import SeedbankError


def search(words):
    if not words:
        raise SeedbankError("give words to search for")
    hits = []
    for row in search_rows(words):
        hits.append((row.get("name", ""), row.get("description", "")))
        print(f"  {row.get('name', '')}")
        if row.get("description"):
            print(f"    {row['description']}")
    return hits
