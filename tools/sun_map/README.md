# Sun map builder

Works out which way every StuyTown / Peter Cooper Village apartment line
faces and how much direct sun it gets on each floor, and writes `data/sun/`
(read by `unit_sun.py`; the map is `data/sun/sun_map.html`).

    pip install -r tools/sun_map/requirements.txt
    python tools/sun_map/build.py          # about a minute

Or run the **Rebuild sun map** workflow from the Actions tab.

## Inputs

| File | What |
| --- | --- |
| `inputs/osm_buildings.json` | Building outlines and walls from OpenStreetMap (© OpenStreetMap contributors, ODbL), with each wall's direction and whether it faces a street or the Oval |
| `inputs/cbre_plans.json` | Apartment labels and 2006 sizes read off the typical-floor plans in CBRE's 2006 offering memorandum (pp. 36–39): 272–280 and 330 First Ave, 400–410 and 442–450 E 20th, 435–445 E 14th, and the Peter Cooper Village typical floor. Each apartment's window directions are in `build.py` (`EXPO`) |
| `inputs/line_notes.json` | Lines you've confirmed yourself. Strongest evidence there is: one confirmed line fixes every letter in its building |
| `data/sun/market_seen.json` | Market-rate listings the watcher saves each morning (view tags, layouts, sizes) |
| `data/events.json` | Affordable listings the watcher has seen |
| `geo.py` | Positions, NOAA sun positions, compass (no dependencies) |
| `template.html` | The map page |

## Method

1. **Plans.** Each CBRE plan is laid onto its building's outline. The labels
   give the apartments' positions, and `EXPO` gives their window walls.
2. **Same design.** Other buildings whose outline matches a planned one after
   turning or mirroring get the same plan. When several ways round fit, the
   listings decide: sizes per letter, and Oval (`VBOV`), East River (`VBRV`)
   and Empire State (`VBESV`) view tags. 647–655 E 14th, 281–287 Ave C and
   245 Ave C / 635–645 E 14th are settled this way.
3. **No plan.** Every floor is 8 apartments round the elevator core, two per
   wing, lettered clockwise. That holds in every plan, and checked against
   the plans this placement matches 70% of lines exactly. Each address's
   core is found from its entrance (the listings API's coordinates) and the
   outline. The evidence then scores each possible spot for A, pooled across
   buildings of the same design:
   - view tags,
   - whether the listed layout is one-sided or two-sided,
   - a prior that in a chain of crosses B/C and F/G sit on the chain wings.

   The result is a probability per spot, not one guess.
4. **Sun.** For every wall piece and floor: the sun every 10 minutes on the
   21st of each month, blocked by every building (its own wings included).
   An apartment is in sun when any of its windows is.
