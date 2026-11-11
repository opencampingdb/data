# Follow OpenStreetMap's formats, and keep what it can't hold as extensions

Where OpenStreetMap documents a format for a tag, the dataset follows it, and the reference OpenStreetMap names for that tag is the judge (opening_hours.js for opening hours, the E.164 international form for phone numbers). A value from OpenStreetMap that is out of spec is kept as OpenStreetMap has it, never fixed or dropped by the pipeline, and is offered to mappers through a MapRoulette challenge. The SDK accepts every value the reference accepts and refuses a traveller's value the reference refuses. Where the dataset holds more than OpenStreetMap can say, it keeps it as an extension: a proposable extension when a mapper could check it at the place, and a candidate for a future tag proposal, or Open Camping DB only when it is an opinion or a personal account, which OpenStreetMap's verifiability rule keeps out. Nothing is proposed to OpenStreetMap without a person deciding to.

## Considered options

- Clean values in the pipeline: rejected, because the dataset would disagree with OpenStreetMap about the same object and mappers would never hear about the problem.
- Drop what OpenStreetMap can't hold: rejected, because travellers' first-hand knowledge would be lost.
- A stricter checker of the dataset's own: rejected after it refused 72 Australian opening hours values that the reference accepts.
