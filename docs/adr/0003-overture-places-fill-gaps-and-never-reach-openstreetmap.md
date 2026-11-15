# Overture places fill gaps and never reach OpenStreetMap

Open Camping DB stays built on OpenStreetMap and takes Overture Maps places as one more fact source, `overture`. An Overture value is only a gap fill: it is taken only where the place has no value from any other source, and a value from OpenStreetMap, a traveller, a contributor, the owner or a community report replaces it. Overture's dates never decide which value wins. Most matched Overture places carry an `update_time` in the current year whether or not anything about them changed, so a newest-wins rule would let one provider's refresh overwrite a mapper's work. Nothing from Overture reaches OpenStreetMap: `overture` values and Overture candidates join the sources that are never sent to OSM, and the MapRoulette challenges leave them out, names included, because the licence of Overture's sources for OpenStreetMap is unchecked. Nothing from Open Camping DB is offered to Overture either: the dataset is under the ODbL, and Overture's places theme has no share-alike, so a contribution would take ODbL data out from under its licence.

## Considered options

- Let the newer of the OpenStreetMap and Overture values win: rejected, because Overture's dates move on its own refreshes, not on changes at the place.
- Send Overture values to mappers as MapRoulette tasks, as traveller reports are: rejected, because a mapper could copy data whose licence for OpenStreetMap nobody has checked.
- Replace OpenStreetMap with Overture as the main source: rejected, because OpenStreetMap holds the camping detail travellers need and Overture does not.
