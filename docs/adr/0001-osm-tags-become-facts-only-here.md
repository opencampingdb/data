# OSM tags become facts only in Open Camping DB

Every rule that turns an OpenStreetMap tag into a fact lives in this repository's pipeline, and apps hold no tag rules of their own. Apps read facts from the release and the feedback SDK. When an app needs something OSM holds, the mapping is added here, so every app reads the same value from the same tag and the evidence for it is recorded once. A mapping goes in only when the OSM wiki's meaning matches the fact and the tag is in use in Australia, and its PR names both, with the date. A fact with no tag in use records the dated check that found none.

## Considered options

- Let each app read tags or names itself, as one app did when it treated a rest area named for trucks as heavy vehicles only. That was rejected: apps would disagree about the same place, and the pipeline already refuses to set a fact from a name alone.
