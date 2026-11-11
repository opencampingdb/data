# Open Camping DB

An open dataset of places to camp and stop, built from OpenStreetMap and from what travellers, owners and contributors report.

## Places

**Place to stay**:
A place of one of the overnight kinds, where travellers can spend the night.
_Avoid_: campsite, accommodation (for the whole group)

## Facts and where they come from

**Fact**:
One thing known about a place, such as whether it has toilets or what it costs. A missing fact means not known, never no.
_Avoid_: attribute, amenity, feature (for the data)

**Mapped fact**:
A fact the dataset can fill from an OpenStreetMap tag. Whether a fact is mapped says nothing about where one place's value came from.
_Avoid_: OSM fact, OSM field

**Fact source**:
Where one place's value for a fact came from: OpenStreetMap, a traveller's report, a contributor, the verified owner, a community report or a rule.
_Avoid_: origin, provenance (for a single value)

**Nearby fact**:
A fact worked out from another OpenStreetMap object inside or near the place, such as the nearest beach or a pub inside a caravan park.
_Avoid_: derived fact

**Rule fact**:
A value set from a place's kind when no source gives one, such as toilets at a public toilet. Any reported value wins over it.

**Extension**:
A fact, kind or value the dataset holds that OpenStreetMap has no tag or value for. It is kept as the dataset's own, never dropped or forced into an OpenStreetMap tag.
_Avoid_: custom field, extra data

**Proposable extension**:
An extension a mapper could check true or false at the place, and so a candidate for a new OpenStreetMap tag.

**Open Camping DB only**:
An extension that is an opinion or a personal account, such as scenery or a review, which OpenStreetMap can never hold.
_Avoid_: private fact

## Quality

**Out of spec**:
A value that the reference OpenStreetMap names for its tag refuses, such as opening hours its reference implementation can't read. A value that only draws a style warning is not out of spec.
_Avoid_: invalid (for a warning), bad data

**First-hand report**:
A report from a traveller who was at the place in the last month: the app saw them nearby, or they said so.
_Avoid_: verified report

## Cost

**Fee**:
The fact that says whether staying costs money: free, fees apply or by donation.
_Avoid_: price, cost category

**Charge**:
A price a place publishes, as OpenStreetMap records it.
_Avoid_: fee, rate

**Fee report**:
What one traveller says they paid a night, for one kind of site, in one month.
_Avoid_: fee, price report
