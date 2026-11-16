"""
Record an apartment line you've confirmed yourself (a viewing, the leasing
office, a photo) in tools/sun_map/inputs/line_notes.json. One confirmed line
fixes every other letter in its building at the next rebuild.

    python tools/sun_map/confirm.py "1 Stuyvesant Oval" 07-F --faces ENE --oval yes
    python tools/sun_map/confirm.py "530 East 20th Street" A --faces SSW WNW --oval no --note "viewing 3/2"
    python tools/sun_map/confirm.py --list

Directions: N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW. Stuy Town's
street grid is turned 29 degrees, so windows looking downtown face SSW, and
toward Avenue C face ESE. Then rebuild: python tools/sun_map/build.py, or the
"Rebuild sun map" workflow.
"""
import argparse
import json
import os
import re
import sys

NOTES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inputs", "line_notes.json")
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record a confirmed apartment line for the sun map.")
    ap.add_argument("address", nargs="?", help='as the listings write it, e.g. "1 Stuyvesant Oval"')
    ap.add_argument("apartment", nargs="?", help="the line letter, or a whole apartment number like 07-F")
    ap.add_argument("--faces", nargs="+", default=[], help="compass directions of its windows")
    ap.add_argument("--oval", choices=["yes", "no"], help="does it look at the Oval?")
    ap.add_argument("--note", default="", help="how you know")
    ap.add_argument("--remove", action="store_true", help="forget this line")
    ap.add_argument("--list", action="store_true", help="show what's recorded")
    args = ap.parse_args(argv)
    with open(NOTES, encoding="utf-8") as f:
        data = json.load(f)
    lines = data.setdefault("lines", {})
    if args.list:
        for key, value in sorted(lines.items()):
            if not key.startswith("EXAMPLE"):
                print(key, json.dumps(value))
        return 0
    if not args.address or not args.apartment:
        ap.error("give an address and an apartment (or --list)")
    letter = args.apartment.strip().upper().split("-")[-1]
    if not re.fullmatch(r"[A-H]", letter):
        ap.error(f"{args.apartment!r}: the line is a letter A-H")
    key = f"{re.sub(r'\s+', ' ', args.address).strip().upper()}|{letter}"
    if args.remove:
        print("removed" if lines.pop(key, None) else "wasn't recorded", key)
    else:
        faces = [f.upper() for f in args.faces]
        bad = [f for f in faces if f not in COMPASS]
        if bad:
            ap.error(f"not a compass direction: {', '.join(bad)}")
        if not faces and not args.oval:
            ap.error("say what you know: --faces and/or --oval")
        entry = {"faces": faces} if faces else {}
        if args.oval:
            entry["oval"] = args.oval == "yes"
        if args.note:
            entry["note"] = args.note
        lines[key] = entry
        print("recorded", key, json.dumps(entry))
    with open(NOTES, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print("Rebuild to use it: python tools/sun_map/build.py (or the 'Rebuild sun map' workflow)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
