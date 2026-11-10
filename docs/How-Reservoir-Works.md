# Reservoir: what this program is, what it is for, and how it works

This guide is written for someone who does not work in software and does not work in solar development. It explains the problem the program is trying to solve, the words people in this industry use, and what the computer actually does.

You do not need to run the program to understand this document. If you later sit with someone who does run it, the last sections describe what you will see on screen.

---

## 1. The simple version

**Reservoir is a research assistant for finding land where a medium-sized solar farm (and often batteries) might actually get built.**

It does not buy land. It does not design panels. It does not get a permit. It does not talk to the electric company on your behalf.

What it does is this:

1. Look at every piece of taxable land in a chosen set of towns.
2. Throw out land that is obviously the wrong kind (houses, cemeteries, schools, protected conservation land, land that is mostly swamp or too steep).
3. Prefer land that is already disturbed: old dumps, contaminated industrial sites, gravel pits, vacant industrial lots.
4. Check whether the nearby electric lines have room for more solar, and how far those lines are.
5. Check, as far as the town’s written rules are known, whether the town even allows this kind of project right now.
6. Hand a small team a ranked list, a one-page briefing for each candidate, and a mailing list of owners to call.

Think of it as **the first filter**, not the final decision. It is closer to “which houses on Zillow are worth a walk-through” than to “sign the purchase-and-sale.”

The program was built for a **two-person origination team**. Origination, in this business, means: find the site, get the owner interested, and start the long process of permits and grid connection before a bigger developer or financier takes it further.

---

## 2. Why this problem exists

A large solar farm in the desert and a few panels on a warehouse roof are well-understood businesses. Reservoir is aimed at something in the middle.

### Medium-sized projects on existing land, near towns

The target is roughly **1 to 5 megawatts** of solar on the ground, often paired with batteries. That is big enough to matter (on the order of hundreds of homes’ electricity, depending on the weather and the time of day) and small enough that it usually connects to the **local distribution lines** — the neighborhood-and-town wires — rather than to the giant transmission towers that carry power across the state.

People in the industry call this **distributed generation**, or **DG**. “Distributed” just means the power is made in many places, close to where it is used, instead of only at a few huge plants.

These projects live or die on a handful of practical questions:

- Is there a piece of land large enough, in one piece, after you subtract swamp, floodplain, steep hillside, and the setbacks the town requires from the property line?
- Is it the *kind* of land people and regulators actually want solar on? An old landfill is often easier to defend than a forest or a farm.
- Can the local electric lines take the power? If the line is already full, you may wait years and pay for upgrades.
- Will the *town* allow it? A town can require a special permit, demand large setbacks, or pass a **moratorium** (a temporary freeze on new applications).

A two-person team cannot drive every road in a utility territory. They also cannot read every town’s zoning book from scratch for every parcel. Reservoir’s job is to do the first pass at both problems, in one place.

### A preference, not a coincidence: already-disturbed land

Massachusetts (and many other places) has been pushing solar toward land that is already “used up”: landfills, cleanup sites, sand and gravel pits, industrial lots. That preference shows up in state incentive programs. Reservoir is built around that pattern on purpose. A pretty hayfield next to a full power line will not outrank a capped landfill with spare room on the feeder just because the hayfield is larger.

---

## 3. Words you will see (plain English)

You can skip this section and come back when a term appears.

**Parcel / tax lot.** One piece of land as the assessor draws it for property tax. It may be too small on its own. Two lots with the same owner that sit next to each other may be one real project. Reservoir calls that grouping an **assemblage**.

**Site.** What Reservoir *ranks*: either one lot, or several neighboring lots glued together because they share an owner.

**Megawatt (MW).** A measure of how much power the plant can produce at full output. Reservoir’s sweet spot is about 1–5 MW. As a rough New England rule of thumb used in this program, it takes about **5 acres of usable land per megawatt**. So a 2 MW project wants on the order of 10 usable acres, not 10 acres of swamp plus a house.

**MWac.** Megawatts on the **AC** (alternating current) side, which is what the grid uses. Panels make DC first; inverters convert it. Reservoir sizes projects in MWac.

**Hosting capacity / headroom.** How many more megawatts of solar the electric company says a given line can accept before it needs a study or an upgrade. “0 MW headroom” means that line is effectively full for new solar right now.

**Feeder.** A distribution circuit: a specific line (or set of lines) coming out of a substation, serving a slice of towns. Reservoir cares about **three-phase** feeders. Those are the heavier local lines that a project of this size typically needs. A house service is not enough.

**Substation.** The facility where those local feeders meet the higher-voltage system. Being closer to one can make interconnection cheaper, but it is not required.

**Interconnection.** The process of getting the utility’s permission to plug in. It is slow, technical, and expensive when the line is constrained. Reservoir only *screens* for obvious grid problems. It does not replace a formal utility study.

**Wetland.** Land that is wet enough, often enough, that environmental rules restrict building. In Massachusetts there is also a **buffer** (this program uses 100 feet) around mapped wetlands. Mapping on a computer is not the same as a biologist walking the site.

**Floodplain / SFHA.** FEMA’s “special flood hazard area” — land with a serious mapped flood risk. Reservoir treats it as not buildable for screening.

**Brownfield.** Land with known or suspected contamination. In Massachusetts, Reservoir looks at **Chapter 21E** sites and **AUL** sites (Activity and Use Limitations: legal restrictions on how the land may be used). These can be good solar sites because the land is already impaired — but you must read the restrictions.

**Landfill.** A dump, closed or active. Solar on a capped landfill is a well-known product, with extra engineering (you cannot drive piles through a cap the usual way). Reservoir flags them so a person checks cap status.

**Setback.** How far from the property line (or from a house) a town says you must keep the equipment. A 50-foot setback on a narrow lot eats a lot of “paper acres.”

**By right / special permit / prohibited / moratorium.**

- **By right:** if you meet the written rules, the town has to allow the use (you still need building and electrical permits).
- **Special permit:** allowed only if a board says yes, which means hearings, conditions, and political risk.
- **Prohibited:** not allowed at this scale.
- **Moratorium:** a pause. Applications are not accepted, or not approved, for a stated period.

**SMART.** A Massachusetts incentive program that pays solar projects for the energy they produce. Later versions of SMART give extra credit to landfill and brownfield projects and penalize clearing forest. Reservoir’s scoring echoes that.

**Energy community / environmental justice (EJ).** Federal and state labels for places that may get extra tax credit or extra scrutiny. Reservoir marks whether a parcel sits in those mapped areas. Eligibility still has to be checked for the current year.

**QGIS.** A free map program. Analysts who want to argue with Reservoir’s drawings can open the same layers there.

---

## 4. What the program is trying to achieve

Reservoir has two jobs that share one screen.

### Job A — Site Screener: “Where should we even look?”

For a chosen region (right now the working example is four Massachusetts towns on National Grid’s system: Uxbridge, Douglas, Northbridge, and Sutton), download public maps, measure each site, and produce:

- a **ranked shortlist** of the most promising sites
- a **one-page site card** (map, acres, grid, owner, warnings)
- an **owner list** grouped for outreach (who to mail, how many sites they have, whether they look like a town, a company, a trust, or a person)

The scoring is not “beauty.” It is a weighted mix of:

| What it cares about | In everyday terms | How much of the score |
|---|---|---|
| Kind of land | Landfill and brownfield beat a farm; houses are out | 30% |
| Room on the electric line | Can this feeder take a project of this size? | 20% |
| Distance to a three-phase line | How far is the wire we need? | 15% |
| Size | Is there enough *usable* land in one piece for a 1–5 MW project? | 10% |
| Town rules | Allowed, special permit, unknown, or frozen? | 10% |
| Incentives | Landfill/brownfield, energy community, low-income area | 8% |
| Forest | Clearing woods is expensive and often politically toxic | 7% |

A site can score well and still be rejected by **hard filters**. Those are pass/fail, not a sliding scale. Examples: the biggest usable piece of land is under 4 acres (2.5 acres if it is a landfill or brownfield); the land is mostly protected open space; the assessor says it is a school or a cemetery.

The team can move the weights (for example, care more about grid headroom and less about land type) and watch the ranking change without downloading the maps again.

### Job B — Siting and moratorium tracker: “Will the town even let us try?”

Town rules kill small projects. They also change: a board passes a 180-day freeze on batteries; a solar bylaw is amended; two towns with similar names get mixed up in news stories.

The tracker is a structured notebook of what each town’s documents actually say: solar allowed or not, batteries allowed or not, setbacks, size caps, and freeze dates.

A language model (Claude) can read a zoning PDF and fill in the notebook. **The program does not trust that fill-in blindly.** Every claimed fact must come with a verbatim sentence from the PDF. Software then checks that the sentence really appears in the file. If it does not, that fact is thrown out or marked unknown. Dates, not the model’s opinion, decide whether a freeze is still on. A *proposed* law is recorded as a watch item and **does not change a site’s score**.

A person can confirm or correct a town in the interface. Once a human has verified a record, the model is not allowed to overwrite it.

There is also an accuracy drill: twenty towns, a person reads the real bylaw, and the program scores itself field by field. Until a person does that work, the accuracy number stays blank on purpose. A fake 94% would be worse than no number.

---

## 5. How the screener works, step by step

You can picture an assembly line.

### Step 1. Choose a region

Someone writes a short settings file: which towns, which state’s data sources, which scoring weights. The working pilot is National Grid’s Blackstone Valley towns in Massachusetts. New York and Oregon are sketched in but not fully run.

### Step 2. Fetch maps (download once, reuse)

The program asks public map servers for, among other things:

- property lines and owner names (the assessor)
- wetlands, flood zones, protected parks and conservation land
- building footprints
- official landfill and contamination sites
- elevation (to compute steep slopes) and land cover (forest vs bare ground vs developed)
- which towns National Grid serves
- three-phase feeders and how much solar they can still take
- substations

The first download for four towns takes on the order of a few minutes. After that, copies are kept on disk so you are not hitting the state’s servers all day.

**A note on the electric map.** Some utility websites will tell you the *numbers* for a feeder (name, headroom, warning flags) but will not give you the actual line as a drawable shape. Reservoir reconstructs an approximate path from the map image the utility does draw. Positions are good to roughly a house lot, not to a survey. That is enough to say “this site is 200 meters from a three-phase line,” not enough to design the trench.

### Step 3. Turn lots into sites (assemblages)

Assessors draw lots for taxes, not for solar. Two 3-acre lots owned by the same gravel company, sharing a fence, are one project in real life.

Reservoir groups lots when **all** of this is true:

- same owner **name and mailing address** (not name alone — otherwise “Town of Uxbridge” would become one giant fake site)
- same town
- they touch, or sit within about 20 meters (so a survey sliver or an unbuilt “paper road” does not split them)
- there is no **mapped public road** running between them (a real road usually means two sites)
- the lot is not a house lot, cemetery, school, golf course, airport, or conservation land

Those lots are then **dissolved**: interior fence lines disappear *before* the program subtracts the setback from the property line. If you set back from every tax line, you would carve a useless strip down the middle of land one owner already controls.

Lots that fail the match (different spelling of the owner, cousin’s LLC next door) stay separate. That is a known limitation.

### Step 4. Measure what is actually usable (“buildable area”)

For each site, Reservoir starts with the outline and subtracts:

- a setback from the outer property line (about 50 feet unless a town’s bylaw has been read and supplies a different number)
- mapped wetlands plus a 100-foot buffer
- FEMA flood hazard areas
- protected open space
- slopes steeper than 15%
- existing buildings plus a small clearance

What remains is **buildable area**. The program then asks: what is the **largest single piece** of that leftover land? Ten acres of leftover in five skinny strips is not a 2 MW site. One 10-acre pad is.

Capacity is estimated as: largest usable piece ÷ 5 acres per megawatt, and not credited above 5 MW (this tool is for the DG band, not a 50 MW plant).

### Step 5. Label what kind of land it is

Order of operations matters. A condominium sitting on an old mill’s contamination listing is still a condominium, not a “brownfield solar site.” Reservoir’s rule of thumb:

1. If the assessor says residential, or the land is a school, cemetery, golf, airport — it is out, even if a contamination point sits on it.
2. Else if a landfill polygon covers a meaningful share of the land (or a landfill point sits on it) — landfill.
3. Else if a 21E or AUL point sits on it — brownfield.
4. Else if the tax code says sand/gravel/quarry — gravel pit.
5. Else if a lot of the land looks like bare ground on the national land-cover map, and the lot is vacant-ish — “disturbed land” (might be an unrecorded pit; check aerial photos).
6. Else use the assessor’s use code (industrial, commercial, farm, town-owned, and so on).

### Step 6. Attach the grid

For each site: nearest three-phase feeder, that feeder’s leftover megawatts (after projects already in line), distance, nearest substation, and warning flags the utility publishes (for example, a regional study that will slow everyone down, or a fee area for system upgrades).

### Step 7. Attach town rules and add up a score

If the tracker has that town, permitting is scored from the written pathway (by right is best; a live solar freeze is nearly a zero). If the town has not been extracted yet, permitting is a neutral middle score so it does not pretend to know.

Seven ingredients are averaged with the weights in the table above. That produces a number from 0 to 100, plus a rank among sites that passed the hard filters, plus a list of plain-language **flags** (“this feeder shows 0 MW headroom — expect upgrade costs,” “half the usable land is forest,” “assessor lists this as conservation land — check Article 97”).

### Step 8. Produce things a person can use on Monday morning

- Spreadsheet of the top sites
- Map file of those sites
- Full map database of *every* site, pass and fail, for someone who wants to see why a favorite lot lost
- Owner spreadsheet for mail merge
- HTML site cards you can print to PDF, one page per site

A first full run of the four-town pilot looked at on the order of **4,700 tax lots**, analysed a few thousand that were large enough, found several hundred that passed the filters, and published a shortlist of 50 sites and about 40 owners. Those counts will move as rules and maps update.

---

## 6. How the tracker works, without the computer jargon

1. A person finds the town’s **current** zoning bylaw or solar/battery local law (the PDF or webpage from the town, not a news paraphrase).
2. They point Reservoir at that file.
3. The reading model fills a form: permission for ground-mounted solar of this size, permission for batteries, setbacks, size caps, freeze dates, and a quote for each claim.
4. Software searches the document for each quote. No quote, no fact.
5. Freeze status is recomputed from calendar dates. A freeze that expired in January is not treated as live in November, but the program will say “check whether they extended it.”
6. A person can click “verified” after they have looked at the source.
7. The screener, on the next score, uses those answers so a freeze or a prohibition actually changes who is at the top of the list.

Until those PDFs are read for a town, **every parcel in that town gets the same middling permitting score.** The tracker tab can look busy while the ranking still ignores town-by-town differences. Filling the tracker is how permitting starts to matter.

---

## 7. What you see if you open the program

The usual path is: someone has already run the screener for a region. You then open a local web page in your browser (it is not a public website; it runs on the analyst’s computer).

Along the side: which region, sliders to re-weight the score, filters (town, land type, minimum size, hide full feeders, hide towns with a freeze).

Across the top, seven tabs:

1. **Map.** Towns, candidate sites colored by score, the leftover “buildable” shape, substations, and feeder dots colored by leftover megawatts (green has room, yellow a little, red none). You can switch to aerial photography.
2. **Shortlist.** The table. Download as a spreadsheet or a map file.
3. **Site cards.** Pick a site; see the one-pager; download it.
4. **Owner outreach.** One row per owner, with mailing address, how many sites, estimated megawatts, best rank. Download for a mail merge. Owner names come from public assessor files and can be a year out of date.
5. **Siting and moratorium tracker.** The town notebook, including quotes and sources. This is also where a person verifies a town or sends a new PDF to be read.
6. **Accuracy check.** The twenty-town scorecard. Empty until humans fill the answer key.
7. **Method and sources.** The recipe and the list of map links, so an outsider can see what was used.

---

## 8. What this program is *not*

Say these out loud before anyone treats a rank as a fact.

- **Not a survey.** Lines are from public maps, good enough to screen, not to build.
- **Not a wetland delineation.** A flag that says “30% wetlands” means “send a scientist,” not “the Conservation Commission already agreed.”
- **Not a title search.** Owner names can lag sales. Always check the current deed before contacting anyone, and before writing an offer.
- **Not a utility study.** Headroom on a hosting-capacity map is a hint. The interconnection queue and a formal pre-application are the real process.
- **Not legal advice.** The tracker is a reading aid. A town planner or land-use lawyer still has to interpret the bylaw for a real application.
- **Not a guarantee of incentives.** SMART adders, federal energy-community credits, and EJ maps change. Confirm the year you will actually qualify.
- **Not complete outside the Massachusetts pilot.** New York and Oregon are partly wired. Do not treat those regions as finished products.

Screening exists to **stop wasting time on the wrong 95%**, and to put the right warnings on the remaining 5%. The remaining work is still human: site visit, owner conversation, title, engineers, town, utility.

---

## 9. Who it is for, and what “good” looks like

**For:** a small origination team that finds sites by looking at land (not by waiting for brokers to send deals), in places where town politics and full distribution feeders are the usual cause of death.

**Success is not** “the computer found the perfect site.” Success looks like:

- Monday: a shortlist you can defend (“we skipped the forest lots and the 0 MW feeders on purpose”).
- Tuesday: printed cards in a folder for a drive-by.
- Wednesday: a mail-merge to 40 owners instead of a scavenger hunt through four towns’ assessor websites.
- Ongoing: a town notebook that does not mix up Cortlandt with Cortlandville, and that does not treat a draft ban as if it were already law.

If a site that looks perfect on the map has a red flag on the card — full feeder, conservation restriction, active freeze — the program has done its job even if that site still “looks good” on aerial photos.

---

## 10. A short story of one place

The first complete run was National Grid territory in the Blackstone Valley of Massachusetts: four towns, thousands of tax lots.

The computer pulled state parcel maps, wetland and flood maps, landfill and cleanup lists, forest and slope, and National Grid’s hosting-capacity information. It grouped some neighboring lots that shared an owner. It threw out house lots and protected land. It ranked what remained with a heavy thumb on dumps, cleanup sites, pits, and industrial land, and with a heavy penalty for lines that are already full.

An analyst can open the map, click a high-ranking vacant industrial site, print a card, and also see why a famous local landfill might *not* be the winner this week — for example because that feeder shows no leftover megawatts, or because the assessor lists conservation (which in Massachusetts can mean a constitutional protection called Article 97).

That is the intended loop: **maps in, judgment still required, less time spent on land that was never going to work.**

---

## 11. If you only remember five things

1. Reservoir finds **candidate land** for medium solar-plus-storage, especially already-disturbed land, near local power lines.
2. It **ranks and warns**. It does not approve, buy, or interconnect.
3. **Usable acres in one piece**, **room on the feeder**, and **town rules** matter more than how big the tax lot looks on an aerial photo.
4. Neighboring lots with the **same owner** can be treated as one site; that is often how real projects are assembled.
5. Town bylaws only affect the ranking after a person points the program at the **real PDF** and (ideally) verifies it. Until then, every town is treated as a question mark.

---

*This describes Reservoir as of November 2026, with a working Massachusetts pilot on National Grid’s Blackstone Valley towns. Technical details, data sources, and commands live in the project README for people who will run the software.*
