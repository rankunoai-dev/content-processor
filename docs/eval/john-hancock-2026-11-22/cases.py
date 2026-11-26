"""Test cases for the John Hancock knowledge base, with ground truth read from the stored pages.

Each question case has:
- `pages`: URL fragments of the page(s) that hold the answer (any one counts);
- `expect`: regular expressions the answer must contain (all of them), taken from the page text;
- `kind`: the intent being tested.

Negative cases (`negative=True`) have no answer on the website: the right behaviour is to
say so (not found) and not to assert anything; `forbid` lists claims that must not appear.
"""

APPLE = "2024/11/john-hancock-adds-new-apple-watch-series-10"
CORRECTION = "2024/12/correction-john-hancock"
OURA = "2023/05/john-hancock-announces-collaboration-with-oura"
LIGHT = "light-the-commonwealth"
SEPT26 = "2026/09/john-hancock-closed-end-funds-declare-monthly-distributions"
OCT26 = "2026/10/john-hancock-closed-end-funds-declare-monthly-distributions"
BTO_JUN26 = "2026/06/john-hancock-financial-opportunities-fund-notice-to-shareholders"
EARN_JUL25 = "2025/07/john-hancock-closed-end-funds-release-earnings-data"
EARLY = "2022/12/john-hancock-closed-end-fund-preferred-income-fund-declares-early"
MEETING25 = "2025/09/john-hancock-closed-end-funds-announce-annual-shareholder-meeting"
REPURCHASE25 = "2025/12/john-hancock-closed-end-funds-announce-renewal-of-share-repurchase"
ESIGN = "how-to-use-esignature"
PRIVACY = "Notice_at_Collection_and_Instructions.pdf"
WALKER = "better-sleep-with-dr-matthew-walker"
COUGHLIN = "the-longevity-opportunity"
HOLMES = "optimizing-human-performance-with-krist"
HANSON = "celebrating-a-decade-of-vitality"
STEELE = "science-of-aging-with-dr-andrew-steele"

CASES = [
    # -- 1. Facts: one fact stated plainly on one page -------------------------------
    dict(
        id="F01",
        kind="fact",
        q="How much is the initial payment for an Apple Watch in the John Hancock Vitality program?",
        pages=[APPLE, CORRECTION],
        expect=[r"\$25"],
    ),
    dict(
        id="F02",
        kind="fact",
        q="How many more points do Apple Watch users earn compared with non-Apple Watch users?",
        pages=[APPLE, CORRECTION],
        expect=[r"7x"],
    ),
    dict(
        id="F03",
        kind="fact",
        q="Which Apple Watch model requires an additional upgrade fee for Vitality PLUS members?",
        pages=[APPLE, CORRECTION],
        expect=[r"Ultra 2"],
    ),
    dict(
        id="F04",
        kind="fact",
        q="What can John Hancock Vitality customers earn rewards for with an Oura Ring?",
        pages=[OURA],
        expect=[r"sleep", r"mindfulness"],
    ),
    dict(
        id="F05",
        kind="fact",
        q="Who is the President and CEO of John Hancock?",
        pages=[LIGHT],
        expect=[r"Brooks Tingle"],
    ),
    dict(
        id="F06",
        kind="fact",
        q="From when to when will John Hancock light its headquarters sign red, white and blue for America's 250th?",
        pages=[LIGHT],
        expect=[r"June 11", r"July 18"],
    ),
    dict(
        id="F07",
        kind="fact",
        q="What is the address of John Hancock's headquarters with the signature sign?",
        pages=[LIGHT, MEETING25],
        expect=[r"200 Berkeley Street"],
    ),
    dict(
        id="F08",
        kind="fact",
        q="What regulatory number was missing from the Apple Watch Series 10 news release?",
        pages=[CORRECTION],
        expect=[r"MLINY102624661-1"],
    ),
    dict(
        id="F09",
        kind="fact",
        q="What phone number can I call to get a life insurance quote without giving consent to be contacted?",
        pages=[COUGHLIN, "longer-healthier-better"],
        expect=[r"888-955-5432"],
    ),
    # -- 2. Numbers in tables and fund notices ---------------------------------------
    dict(
        id="N01",
        kind="number",
        q="What monthly distribution per share did the Premium Dividend Fund (PDT) declare on September 1, 2026?",
        pages=[SEPT26],
        expect=[r"0\.0883"],
    ),
    dict(
        id="N02",
        kind="number",
        q="What was the payment date of the closed-end fund monthly distributions declared on September 1, 2026?",
        pages=[SEPT26],
        expect=[r"September 30, 2026"],
    ),
    dict(
        id="N03",
        kind="number",
        q="What was the annualized distribution rate at market of the Tax-Advantaged Dividend Income Fund (HTD) in the September 2026 declaration?",
        pages=[SEPT26],
        expect=[r"7\.52"],
    ),
    dict(
        id="N04",
        kind="number",
        q="How much was the quarterly distribution per share of the Financial Opportunities Fund (BTO) paid on June 30, 2026?",
        pages=[BTO_JUN26],
        expect=[r"0\.6500"],
    ),
    dict(
        id="N05",
        kind="number",
        q="What share of BTO's June 2026 distribution came from net realized long-term capital gains?",
        pages=[BTO_JUN26],
        expect=[r"74\s?%"],
    ),
    dict(
        id="N06",
        kind="number",
        q="What was the Hedged Equity & Income Fund's net investment income per common share for the three months ended June 30, 2025?",
        pages=[EARN_JUL25],
        expect=[r"0\.207"],
    ),
    dict(
        id="N07",
        kind="number",
        q="What was the NAV of the Financial Opportunities Fund (BTO) for the three months ended June 30, 2025?",
        pages=[EARN_JUL25],
        expect=[r"33\.84"],
    ),
    dict(
        id="N08",
        kind="number",
        q="Up to what percentage of outstanding common shares may each fund repurchase in 2026 under the renewed share repurchase plans?",
        pages=[REPURCHASE25],
        expect=[r"10\s?%"],
    ),
    dict(
        id="N09",
        kind="number",
        q="When will the 2026 annual meeting of shareholders of the John Hancock closed-end funds be held?",
        pages=[MEETING25],
        expect=[r"February 17, 2026"],
    ),
    dict(
        id="N10",
        kind="number",
        q="What was the distribution per share of Preferred Income Fund II (HPF) declared on September 1, 2026?",
        pages=[SEPT26],
        expect=[r"\$0\.\d{4}"],
        note="The stored table has no per-share value in the HPF row (an extraction gap to confirm on the live page).",
    ),
    # -- 3. Explanations (why / how) --------------------------------------------------
    dict(
        id="E01",
        kind="explain",
        q="Why did the Preferred Income Fund declare its January 2023 distribution early in December 2022?",
        pages=[EARLY],
        expect=[r"excise tax"],
    ),
    dict(
        id="E02",
        kind="explain",
        q="What are the four macros of good sleep according to Dr. Matthew Walker?",
        pages=[WALKER],
        expect=[r"Quantity", r"Quality", r"Regularity", r"Timing"],
    ),
    dict(
        id="E03",
        kind="explain",
        q="According to Joe Coughlin, what is the key to living longer better?",
        pages=[COUGHLIN],
        expect=[r"social connection"],
    ),
    dict(
        id="E04",
        kind="explain",
        q="What do I need to upload if I want my annuity withdrawal paid by electronic fund transfer through the online form?",
        pages=[ESIGN],
        expect=[r"voided check"],
    ),
    dict(
        id="E05",
        kind="explain",
        q="Which privacy law is the John Hancock Notice at Collection written for?",
        pages=[PRIVACY],
        expect=[r"California Consumer Privacy Act"],
    ),
    # -- 4. Eligibility rules and conditions -------------------------------------------
    dict(
        id="R01",
        kind="rule",
        q="Above what withdrawal amount is electronic submission of an annuity service form not available?",
        pages=[ESIGN],
        expect=[r"\$250,000"],
    ),
    dict(
        id="R02",
        kind="rule",
        q="If I changed my address of record recently, can I use the online form to get a check sent to that address?",
        pages=[ESIGN],
        expect=[r"15 days"],
    ),
    dict(
        id="R03",
        kind="rule",
        q="Does John Hancock sell my personal information to third parties?",
        pages=[PRIVACY],
        expect=[r"(do not|We do not)[\s\S]{0,80}sell"],
    ),
    # -- 5. People and roles ------------------------------------------------------------
    dict(
        id="P01",
        kind="people",
        q="Where does Kristen Holmes work and what is her role?",
        pages=[HOLMES],
        expect=[r"WHOOP"],
    ),
    dict(
        id="P02",
        kind="people",
        q="What is Lindsay Hanson's job title at John Hancock?",
        pages=[HANSON, APPLE, CORRECTION],
        expect=[r"Chief Marketing Officer"],
    ),
    dict(
        id="P03",
        kind="people",
        q="Which podcast episode is about the science of aging and altering the biological processes of aging?",
        pages=[STEELE],
        expect=[r"Andrew Steele"],
        note="Every podcast page also carries an Andrew Steele teaser, so many pages compete.",
    ),
    # -- 6. Exact codes and paraphrase (how retrieval copes) ---------------------------
    dict(
        id="K01",
        kind="keyword",
        q="ACHEFTFM",
        pages=["how-to-use-esignature-ACHEFTFM"],
        expect=[r"ACHEFTFM"],
    ),
    dict(
        id="K02",
        kind="keyword",
        q="HPS distribution per share October 2026",
        pages=[OCT26],
        expect=[r"0\.1100"],
    ),
    dict(
        id="K03",
        kind="paraphrase",
        q="Is there a deal on a smart ring that tracks how I sleep?",
        pages=[OURA],
        expect=[r"(Oura|ŌURA|OURA)"],
    ),
    dict(
        id="K04",
        kind="paraphrase",
        q="¿Cuánto hay que pagar al principio por un Apple Watch con Vitality?",
        pages=[APPLE, CORRECTION],
        expect=[r"\$25"],
        note="Spanish question (the model is multilingual).",
    ),
    # -- 7. Recency: many near-identical releases ---------------------------------------
    dict(
        id="T01",
        kind="recency",
        q="What is the latest monthly distribution declared by the John Hancock closed-end funds?",
        pages=[OCT26],
        expect=[r"October 1, 2026|Oct\. 1, 2026"],
        note="The newest release in the scrape is 1 October 2026; 59 similar releases compete.",
    ),
    dict(
        id="T02",
        kind="recency",
        q="What was the Premium Dividend Fund monthly distribution per share in 2026?",
        pages=["/2026/"],
        expect=[r"0\.0883"],
    ),
    # -- 8. Not on the website: the system must not invent an answer ------------------
    dict(
        id="X01",
        kind="negative",
        negative=True,
        q="How much does John Hancock term life insurance cost per month for a 40-year-old?",
        forbid=[r"\$\d+ per month for"],
    ),
    dict(
        id="X02",
        kind="negative",
        negative=True,
        q="Does John Hancock offer pet insurance?",
        forbid=[r"pet insurance"],
    ),
    dict(
        id="X03",
        kind="negative",
        negative=True,
        q="Who is the CEO of Manulife?",
        forbid=[r"CEO of Manulife"],
    ),
    dict(
        id="X04",
        kind="negative",
        negative=True,
        q="Why did John Hancock shut down the Vitality program in 2024?",
        forbid=[r"shut down|ended the Vitality"],
        note="False premise: Vitality was not shut down.",
    ),
]
