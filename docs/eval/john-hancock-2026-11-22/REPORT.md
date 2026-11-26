# RAG and modules test: John Hancock (22 November 2026)

## 1. Summary

**56 test cases** on the John Hancock project: 40 questions put to the knowledge base, 4 tests of the citation checker that guards Claude Desktop's answers, and 12 tests of the module tools. Each answer was compared with the text stored for the page that holds the answer; every expected answer below was copied from that stored text.

| What was measured | Result |
|---|---|
| **Answer accuracy** (the app's answer contains the right fact) | **15 of 36 (42%)** |
| Right page among the first 3 search results | 28 of 36 (78%) |
| Right page among the first 10 search results | 30 of 36 (83%) |
| Right page as the first search result | 18 of 36 (50%) |
| The right fact was in the passages the answer step had | 28 of 36 (78%) |
| **Quotes copied word for word** (no invented text) | **81 of 81 (100%)** |
| Questions not answered on the website: said "not found" | **0 of 4** |
| Citation checker behaved correctly | 3 of 4 |
| Module tools behaved correctly | 9 of 12 (1 more partly) |
| Speed (median): answer / search | 0.17 s / 0.42 s |

**In one sentence:** the system never makes anything up and usually finds the right page, but the step that turns the found passages into an answer picks the wrong sentence more often than the right one, cannot read numbers out of tables, ignores dates, and never says "I don't know".

The biggest single lever: in 13 of the 21 wrong answers the right fact was already in the passages the answer step was given. The fault is the sentence choice, not the search.

## 2. What was tested, and how

**The system's intent.** The knowledge base should answer questions about a client's (and competitors') websites from the captured pages only, with every statement backed by a word-for-word quote and a link to the page; say when the website does not answer; and let Claude Desktop search, read pages and have its answer checked before it reaches a person. The modules (themes, monitoring, gaps, research, catalog, SEO, duplicates) should report facts that match the stored pages, and say plainly when they lack the data.

**Data.** The John Hancock project: one website (johnhancock.com), one scrape (26-27 October 2026), 272 searchable pages in 1,308 passages: 249 news releases (242 of them closed-end fund notices, many near-identical month after month), 18 podcast and docuseries pages, 4 annuity e-signature help pages, 1 privacy PDF. Index model: the CPU meaning model (`paraphrase-multilingual-MiniLM-L12-v2`).

**The two answer paths tested, for every question:**

1. **The app's answer step** (`answer_question`, what the knowledge base's ask command does): hybrid search (meaning plus exact words), the best 5 passages plus their neighbours, then an answer. No local instruct model is set up (Ollama is not configured yet), so answers are **extractive**: the sentences that share the most words with the question, quoted as they are. This is the answer a user gets today.
2. **Claude Desktop's search tool** (`search_knowledge`, top 10): scored on whether the right page is returned and how high. With Claude reading these passages, the answer can only be as good as what search returns.

**Grading.** A question passes when the answer contains the expected fact (checked by pattern against the answer text, then every result reviewed by hand against the stored page). Every quote was checked word for word against the stored page text, independently of the app's own check. For the 4 questions the website does not answer, a pass is "not found" with no false claim.

**Intents covered (40 questions):** facts (9), numbers in tables and fund notices (10), explanations (5), rules and conditions (3), people and roles (3), exact codes (2), paraphrase and another language (2), the most recent of many similar pages (2), questions the website does not answer, including a false premise (4).

## 3. What works

- **No invented content.** All 81 answer statements are exact quotes of stored text. Even on the four unanswerable questions the system quoted real sentences rather than inventing a price, a pet insurance product or a reason Vitality "shut down".
- **Search finds the right page most of the time.** 78% in the first 3, 83% in the first 10. Explanations (5 of 5), people (3 of 3) and rules (3 of 3) were always in the first 3.
- **Meaning search works across wording and language.** "A deal on a smart ring that tracks how I sleep" reached the Oura page; a Spanish question about the Apple Watch reached the Apple Watch release first.
- **Prose answers are right.** Explanations 4 of 5, people 3 of 3: when the answer is a full sentence that shares words with the question, the extractive answer gets it.
- **The citation checker catches tampering.** A number changed inside a quote ($25 to $15, 0.0883 to 0.0983) and a citation to a passage never shown were both refused.
- **Modules say what they lack.** Gaps reports "no keyword data yet"; research tools refuse with what to do; monitoring keeps the first scrape as a baseline; changes need two scrapes and say so. Page facts and page text match the stored page exactly.

## 4. What fails, and why (most impact first)

### 4.1 The answer picks the wrong sentence (13 cases)

F01, F03, F06, F09, N02, N05, N06, N07, E05, R01, R03, K04, T02. In K01 the right page was found first but the answer step found no sentence and said not found (the form code is only in the page's title and address). The answer step scores each sentence by how many question words it shares. Headlines, titles and column labels share many words and win: "How much is the initial payment for an Apple Watch..." was answered with "John Hancock Adds New Apple Watch Series 10 to its Vitality Program" while "...make an initial payment of $25, plus tax..." sat in the same passage. A bullet can lose its meaning when quoted alone: R03 quoted "sell your Personal Information... to third parties" without its lead-in "We do not:", which reads as the opposite.

*What would fix it:* the designed path, a local instruct model (Ollama) writing the answer with the code check on every quote; until then, a better extractive scorer: skip headings, titles and lines of under 6 words; prefer sentences with a number or date when the question asks how much, when or what rate; quote a bullet with its lead-in.

### 4.2 Numbers in tables cannot be answered (N03, N05, N06, N07, N10, K02, T02)

Tables are stored one cell per line ("PDT", "Premium Dividend Fund", "$0.0883", "$12.61", "8.40 %"), so a value is no longer next to its fund or column name. Neither the sentence picker nor keyword search can tie "7.52 %" to HTD, or "$33.84" to BTO's NAV. The fund notices are mostly tables, so this hits the largest part of the John Hancock site.

**Data gap (N10):** in the September and October 2026 distribution tables, the Preferred Income Fund II (HPF) row has no distribution per share: the stored row goes from the fund name straight to the market price, so the columns after it are shifted. Worth checking against the live page and the table extraction.

*What would fix it:* store each table row as one line with its headers ("HPF | Preferred Income Fund II | Distribution per share $0.1237 | Market price $15.57 | Rate 9.52 %"), and keep empty cells as empty instead of dropping them.

### 4.3 Dates and "latest" are ignored (N01, N03, K02, T01)

There are about 60 near-identical distribution releases. A question naming "September 1, 2026" or "October 2026", or asking for "the latest", got other months, because nothing in search uses the page's publication date. T01 ("the latest monthly distribution") returned 2025 releases and a podcast; the newest (1 October 2026) was not in the first 10.

*What would fix it:* read a date or "latest/most recent" in the question; filter or boost by the page's published date (already in every page stamp); for "latest", sort the matching pages by date.

### 4.4 It never says "not found" (X01-X04: 0 of 4)

There is no relevance threshold: some sentences always come back. Usually harmless disclaimers ("Insurance products are issued by..."), but X03 is misleading: asked for **Manulife's** CEO, it quoted "Brooks Tingle, President and CEO of **John Hancock**". The false premise in X04 (Vitality "shut down") was not corrected.

*What would fix it:* an answerability check: say "not found on the website" when the best passage's meaning score is below a threshold or no sentence covers the question's key terms (the entity asked about, the product asked about).

### 4.5 Some entity questions are missed by search (F05, F07)

"Who is the President and CEO of John Hancock?" ranked the right release 8th (behind fund notices that mention "President" in boilerplate). The headquarters address (200 Berkeley Street) was not in the first 10. Both facts sit in one sentence of a long release.

### 4.6 Repeated text competes with real content

Every podcast page begins with the same Andrew Steele teaser and ends with the same disclaimers; the Apple Watch release exists three times (the original and two addresses of the correction notice). These copies take places in the top results. The semantic report already removes such site furniture; the index does not.

### 4.7 The citation checker lets short uncited claims through (1 of 4 failed)

An answer with four extra claims and no citations ("John Hancock is the largest insurer in Canada.", "Members can also earn a free Peloton bike.", ...) **passed**: a line counts as a claim only when it has a number or at least 14 words, and up to 3 uncited claims are allowed. Two of the four were not even counted.

*What would fix it:* count every sentence with a named product, company or figure as a claim; allow 0 uncited claims outside the exempt sections (or flag them in the result even when it passes).

### 4.8 Module tools (3 issues)

- **find_similar_pages** returns at most 40 pairs, and all 40 are recurring fund notices at similarity 1.0 (the same notice for a different month). The actionable duplicates (the correction notice that republishes the Apple Watch release under two addresses; the near-identical e-signature form pages) are not listed. It should set recurring series aside, as the semantic similarity report does.
- **list_pages** shows 37 of 318 pages and says so ("showing 37 of 75; narrow the request"), but has no way to ask for the next part (only a website can be given).
- **Data gap** in the HPF rows (see 4.2).

Themes, monitoring, gaps and research were checked only for the honest "not enough data" behaviour: the project has one website and one scrape and no search data, so competitor themes, change alerts, gap findings and rankings could not be tested for their real purpose yet.

## 5. Limits of this test

- Answers were produced without a local instruct model (none is configured), so this measures the extractive fallback and the search; it does not measure the model-written answer path or Claude Desktop's own answers.
- One website, one scrape, no competitor, no research data: the competitive and change-tracking modules could only be tested for correct refusals.
- 40 questions on 272 pages is a sample: the percentages are indicative (each question is 2.8 points).
- Expected answers come from the stored page text; the live website was not fetched, so where the stored text itself is wrong (the HPF rows) the test cannot tell what the live page says.

## 6. Results, case by case

### Facts (3 of 9 passed)

| Case | Question | Expected (from the page) | What the system answered | Right page in search | Result | Why |
|---|---|---|---|---|---|---|
| F01 | How much is the initial payment for an Apple Watch in the John Hancock Vitality program? | $25, plus tax (initial payment) | To date, Apple Watch has been an extremely popular and effective aspect of the John Hancock Vitality Program. | 2 | **Fail** | Right pages found (rank 2) but the answer picked the release's opening and title lines, not the $25 sentence. |
| F02 | How many more points do Apple Watch users earn compared with non-Apple Watch users? | roughly 7x more points | A recent analysis of John Hancock Vitality members showed that Apple Watch users are more engaged in the program, earning roughly 7x more points in t… | 1 | **Pass** | Correct sentence quoted from the Apple Watch release. |
| F03 | Which Apple Watch model requires an additional upgrade fee for Vitality PLUS members? | Apple Watch Ultra 2 | Beginning this November, John Hancock Vitality PLUS members can earn Apple Watch Series 10, or Apple Watch SE, for as little as $25, plus tax, by sta… | 1 | **Fail** | Right page first, but the answer quoted the Series 10 / SE sentence; the Ultra 2 is in the sentence right after it. |
| F04 | What can John Hancock Vitality customers earn rewards for with an Oura Ring? | healthy sleep and mindfulness | Through this collaboration, eligible John Hancock Vitality customers who have an Oura Ring and active Oura Membership will be able to connect to the … | 2 | **Pass** | Correct (Oura page quoted on sleep and mindfulness). |
| F05 | Who is the President and CEO of John Hancock? | Brooks Tingle | JOHN HANCOCK HEDGED EQUITY & INCOME FUND - REQUIRED NOTICE TO SHAREHOLDERS - SOURCES OF DISTRIBUTION UNDER SECTION 19(a) | 8 | **Fail** | Missed: the CEO is named only inside a quote in the Light the Commonwealth release; search ranked it 8th, behind fund notices that mention 'President'. |
| F06 | From when to when will John Hancock light its headquarters sign red, white and blue for America's 250th? | June 11 to July 18 | John Hancock, Governor Healey, and Mayor Wu Call on Community to Light the Commonwealth Red, White, and Blue for America's 250th | 1 | **Fail** | Right page first, but the answer quoted the headline and the mayor's quote; the dates sentence was not chosen. |
| F07 | What is the address of John Hancock's headquarters with the signature sign? | 200 Berkeley Street | Can a John Hancock client services representative (CSR) fill out a form online and then email it to a contract owner to review and sign? | not in top 10 | **Fail** | Missed: search returned the e-signature help pages; the 200 Berkeley Street sentence was not in the top 10. |
| F08 | What regulatory number was missing from the Apple Watch Series 10 news release? | MLINY102624661-1 | In the news release, John Hancock Adds New Apple Watch Series 10 to its Vitality Program, issued 21-Nov-2024 by John Hancock over PR Newswire, we are… | 1 | **Pass** | Correct: the correction notice quoted with the regulatory number. |
| F09 | What phone number can I call to get a life insurance quote without giving consent to be contacted? | 888-955-5432 | By selecting "Stay Connected" I am providing my express written consent to receive emails and phone calls (if applicable based on my selection) (usin… | 5 | **Fail** | The number is on the docuseries sign-up form; the answer quoted the consent text without the number. |

### Numbers and tables (3 of 10 passed)

| Case | Question | Expected (from the page) | What the system answered | Right page in search | Result | Why |
|---|---|---|---|---|---|---|
| N01 | What monthly distribution per share did the Premium Dividend Fund (PDT) declare on September 1, 2026? | $0.0883 | Premium Dividend Fund (the "Fund") declared its monthly distribution pursuant to the Fund's managed distribution plan (the "PDT Plan"). | not in top 10 | **Fail** | Missed: 60 near-identical distribution releases; the date in the question is not used, and search returned other months (and a podcast). |
| N02 | What was the payment date of the closed-end fund monthly distributions declared on September 1, 2026? | September 30, 2026 | BOSTON, Sept. 1, 2026 /CNW/ -- The John Hancock closed-end funds listed below declared their monthly distributions today as follows: | 2 | **Fail** | Right page at rank 2, but the answer quoted the 'declared today' sentence, not the Payment Date line. |
| N03 | What was the annualized distribution rate at market of the Tax-Advantaged Dividend Income Fund (HTD) in the S… | 7.52 % | Tax-Advantaged Dividend Income Fund | not in top 10 | **Fail** | Missed: wrong months returned; the rate lives in a flattened table cell ('7.52 %') with no words around it. |
| N04 | How much was the quarterly distribution per share of the Financial Opportunities Fund (BTO) paid on June 30, … | $0.6500 | BOSTON, June 30, 2026 /CNW/ - John Hancock Financial Opportunities Fund (NYSE: BTO) (the "Fund"), a closed-end fund managed by John Hancock Investmen… | 1 | **Pass** | Correct: the BTO notice sentence with $0.6500. |
| N05 | What share of BTO's June 2026 distribution came from net realized long-term capital gains? | 74 % | The following table sets forth the estimated sources of the current distribution, payable June 30, 2026, and the cumulative distributions paid this f… | 2 | **Fail** | Right page at rank 2, but 74 % is a lone table cell; the answer quoted the table's introduction. |
| N06 | What was the Hedged Equity & Income Fund's net investment income per common share for the three months ended … | $0.207 | Hedged Equity & Income Fund | 2 | **Fail** | Right page at rank 2, but $0.207 is a lone table cell; the answer quoted a fund name cell from another year. |
| N07 | What was the NAV of the Financial Opportunities Fund (BTO) for the three months ended June 30, 2025? | $33.84 | Average annual total return (in relation to NAV) for the 5 years ended on May 31, 2025 | 2 | **Fail** | Right page at rank 2, but $33.84 is a lone table cell; the answer quoted NAV lines of other notices. |
| N08 | Up to what percentage of outstanding common shares may each fund repurchase in 2026 under the renewed share r… | up to an additional 10% | Under the share repurchase plans, each Fund may purchase, in the open market, between January 1, 2026, and December 31, 2026, up to an additional 10%… | 2 | **Pass** | Correct: the repurchase sentence with 10%. |
| N09 | When will the 2026 annual meeting of shareholders of the John Hancock closed-end funds be held? | February 17, 2026, 2:00 p.m. ET | BOSTON, Sept. 25, 2025 /CNW/ - The nine John Hancock closed-end funds listed below announced today that each fund's Annual Meeting of Shareholders ("… | 1 | **Pass** | Correct: the annual meeting sentence with the date. |
| N10 | What was the distribution per share of Preferred Income Fund II (HPF) declared on September 1, 2026? | (not in the stored table) | Distribution Per Share | not in top 10 | **Fail** | Data gap: the stored September 2026 table has no per-share value in the HPF row (cells shifted); the October table has the same gap. |

### Explanations (4 of 5 passed)

| Case | Question | Expected (from the page) | What the system answered | Right page in search | Result | Why |
|---|---|---|---|---|---|---|
| E01 | Why did the Preferred Income Fund declare its January 2023 distribution early in December 2022? | to meet the 2022 distribution requirement for federal excise tax purp… | JOHN HANCOCK CLOSED-END FUND PREFERRED INCOME FUND DECLARES EARLY MONTHLY DISTRIBUTION | 2 | **Pass** | Correct: the excise tax sentence. |
| E02 | What are the four macros of good sleep according to Dr. Matthew Walker? | Quantity, Quality, Regularity, Timing | "Four macros" of good sleep — Quantity, Quality, Regularity, and Timing (QQRT) — and emphasizes the importance each macro for overall well-being | 1 | **Pass** | Correct: the QQRT sentence. |
| E03 | According to Joe Coughlin, what is the key to living longer better? | social connection | While new technologies can make aging easier, he believes social connection is the key to living longer better. | 1 | **Pass** | Correct: the social connection sentence. |
| E04 | What do I need to upload if I want my annuity withdrawal paid by electronic fund transfer through the online … | a voided check | If you elect to receive funds via an electronic fund transfer to your checking account, you must scan and save a voided check to your computer’s desk… | 1 | **Pass** | Correct: the voided check sentence. |
| E05 | Which privacy law is the John Hancock Notice at Collection written for? | California Consumer Privacy Act / CPRA | If you have any questions about this Notice, please contact the John Hancock Privacy Office at privacy@jhancock.com | 1 | **Fail** | Right document first, but the answer quoted the contact line and a category list, not the CCPA sentence. |

### Rules and conditions (1 of 3 passed)

| Case | Question | Expected (from the page) | What the system answered | Right page in search | Result | Why |
|---|---|---|---|---|---|---|
| R01 | Above what withdrawal amount is electronic submission of an annuity service form not available? | $250,000 | Now you can securely complete and submit certain service forms entirely online using an electronic signature. | 1 | **Fail** | Right page first, but the answer quoted the introduction; the $250,000 bullet was not chosen. |
| R02 | If I changed my address of record recently, can I use the online form to get a check sent to that address? | not if changed within the last 15 days | The address of record (AOR) has been changed within the last 15 days and the requested delivery method is a check to the AOR (service can be used for… | 1 | **Pass** | Correct: the 15 days bullet. |
| R03 | Does John Hancock sell my personal information to third parties? | No: "We do not sell your Personal Information" | • sell your Personal Information including any sensitive personal information to third parties for monetary consideration, • share it with third part… | 3 | **Fail** | The answer quoted the bullet ('sell your Personal Information...') without its lead-in 'We do not:', which reads as the opposite; the second point is about DocuSign. |

### People and roles (3 of 3 passed)

| Case | Question | Expected (from the page) | What the system answered | Right page in search | Result | Why |
|---|---|---|---|---|---|---|
| P01 | Where does Kristen Holmes work and what is her role? | WHOOP, Vice President of Performance | Kristen Holmes, Vice President of Performance at WHOOP, shares her insights on optimizing human performance through understanding personal data, alig… | 1 | **Pass** | Correct: Kristen Holmes, WHOOP. |
| P02 | What is Lindsay Hanson's job title at John Hancock? | Chief Marketing Officer and Global Head of Behavioral Insurance Strat… | Better., host David O’Leary welcomes Lindsay Hanson, Chief Marketing Officer and Global Head of Behavioral Insurance at John Hancock. | 1 | **Pass** | Correct. |
| P03 | Which podcast episode is about the science of aging and altering the biological processes of aging? | Dr. Andrew Steele episode | Andrew Steele presents an exciting new frontier of science — how we can potentially alter the biological processes of aging in our bodies. | 1 | **Pass** | Correct, although every podcast page carries the same Andrew Steele teaser. |

### Exact codes (0 of 2 passed)

| Case | Question | Expected (from the page) | What the system answered | Right page in search | Result | Why |
|---|---|---|---|---|---|---|
| K01 | ACHEFTFM | the ACHEFTFM online submission form page | (not found) | 1 | **Fail** | Search found the page first (the code is in its title and address), but the answer step found no sentence with the code and said not found. |
| K02 | HPS distribution per share October 2026 | $0.1100 | Distribution Per Share | not in top 10 | **Fail** | Missed: the October 2026 release was not returned; other months' tables were. |

### Paraphrase and other language (1 of 2 passed)

| Case | Question | Expected (from the page) | What the system answered | Right page in search | Result | Why |
|---|---|---|---|---|---|---|
| K03 | Is there a deal on a smart ring that tracks how I sleep? | the Oura Ring collaboration | Guidance on emerging sleep trends and technologies like the Oura Ring or a smart mattress | 3 | **Pass** | Correct: the paraphrase ('smart ring that tracks how I sleep') reached the Oura page. |
| K04 | ¿Cuánto hay que pagar al principio por un Apple Watch con Vitality? | $25 (Spanish question) | John Hancock Adds New Apple Watch Series 10 to its Vitality Program | 1 | **Fail** | The Spanish question reached the right pages first, but the answer quoted headlines, not the $25 sentence. |

### Most recent of many (0 of 2 passed)

| Case | Question | Expected (from the page) | What the system answered | Right page in search | Result | Why |
|---|---|---|---|---|---|---|
| T01 | What is the latest monthly distribution declared by the John Hancock closed-end funds? | the 1 October 2026 declaration | Premium Dividend Fund (the "Fund") declared its monthly distribution pursuant to the Fund's managed distribution plan (the "PDT Plan"). | not in top 10 | **Fail** | Missed: nothing in search prefers the newest release; older months and a podcast were returned. |
| T02 | What was the Premium Dividend Fund monthly distribution per share in 2026? | $0.0883 | Distribution Per Share | 1 | **Fail** | A 2026 page was found first, but the answer quoted the 'Distribution Per Share' column label. |

### Not on the website (0 of 4 passed)

| Case | Question | Expected (from the page) | What the system answered | Right page in search | Result | Why |
|---|---|---|---|---|---|---|
| X01 | How much does John Hancock term life insurance cost per month for a 40-year-old? | not on the website | GO and a 15 percent discount for eligible life insurance customers participating in John Hancock Vitality | - | **Fail** | No abstention: quoted disclaimers and a 15 percent discount line instead of saying the price is not on the site. Nothing invented. |
| X02 | Does John Hancock offer pet insurance? | not on the website | Insurance products are issued by: John Hancock Life Insurance Company (U.S.A.), Boston, MA 02116 (not licensed in New York) and John Hancock Life Ins… | - | **Fail** | No abstention: quoted disclaimers. Nothing invented. |
| X03 | Who is the CEO of Manulife? | not on the website | "As we reflect on our city, state, and nation's history, we're also celebrating the spirit of innovation that has carried us through moments of signi… | - | **Fail** | No abstention, and misleading: quoted 'Brooks Tingle, President and CEO of John Hancock' for a question about Manulife's CEO. |
| X04 | Why did John Hancock shut down the Vitality program in 2024? | false premise: Vitality was not shut down | In the news release, John Hancock Adds New Apple Watch Series 10 to its Vitality Program, issued 21-Nov-2024 by John Hancock over PR Newswire, we are… | - | **Fail** | No abstention: quoted Vitality sentences without correcting the false premise. Nothing invented. |

## 7. Citation checker (what guards Claude Desktop's answers)

The passages were shown through the same tools Claude Desktop uses (`get_page`), then four versions of an answer were checked with the app's own checker (`check_deliverable`'s code).

| Case | Expected | Got | Result |
|---|---|---|---|
| Honest answer, two exact quotes with their references | passed | passed | **Pass** |
| A number changed inside each quote ($25 to $15, 0.0883 to 0.0983) | needs review | needs review (2 quotes failed) | **Pass** |
| A reference to a passage that was never shown | needs review | needs review (1 unknown reference) | **Pass** |
| The honest answer plus four claims with no citation | needs review | **passed** (2 of the 4 counted as claims) | **Fail** |

## 8. Module tools

| Case | Check | Result | What was found |
|---|---|---|---|
| M01 | list_pages lists the website's pages | **Partly** | 37 of 318 pages shown; it says the list is cut ("narrow the request") but offers no way to get the rest |
| M02 | get_page_facts matches the stored page (title, H1, description) | **Pass** | identical |
| M03 | get_page returns all sections in order, with the facts | **Pass** | 4 of 4 sections; $25 and 7x present |
| M04 | get_offering_catalog returns the offering tree | **Pass** | 20 offerings from the menus (life insurance, term, permanent, Vitality, annuities, IRAs, college savings and more); only 4 pages are attached to them, as this scrape covered news, insights and help pages, not product pages |
| M05 | get_seo_issues counts agree with the stored pages | **Pass** | 252 pages without a meta description (stored data: 250 of the 272 searchable pages, 252 of all 318 addresses); 284 titles over 60 characters |
| M06 | find_similar_pages shows real duplicates | **Fail** | 40 pairs, all recurring fund notices at 1.0; the correction re-publishing the Apple Watch release and the near-identical e-signature forms are not listed |
| M07 | get_theme_map groups the pages | **Pass** (labels weak) | 8 themes (130, 79, 32, 18, 4, 3, 2, 2 pages), all "client only" as there is no competitor. The groups are sensible, but each is named after one of its pages: the 18 podcast pages are "Podcast: Dr. Lisa Genova on Why Forgetting Is Healthy", and the 130 notices of five different funds are "JOHN HANCOCK PREMIUM DIVIDEND FUND NOTICE TO SHAREHOLDERS" |
| M08 | get_monitoring_alerts with one scrape | **Pass** | baseline kept, no alerts |
| M09 | get_changes with one scrape | **Pass** | "changes need two (scrape it again to compare)" |
| M10 | get_gap_findings without competitors or research | **Pass** | no findings, with the reason |
| M11 | get_keyword_rankings without research | **Pass** | refused, with what to import |
| M12 | The September 2026 table has HPF's distribution per share | **Fail** | the stored row has no value (data gap) |

## 9. Recommended next steps, in order

1. **Configure the local instruct model (Ollama)** so answers are written from the passages and every quote is checked; then run this test again. Search already puts the fact in front of the answer step in 78% of questions.
2. **Store table rows with their headers** (and keep empty cells), then re-index John Hancock: the fund notices are most of the site.
3. **Make search date-aware:** dates and "latest" in the question, against each page's published date.
4. **Add "not found on the website"** when nothing relevant enough comes back.
5. **Remove site furniture and duplicate copies at index time** (the semantic report's rule).
6. **Tighten the citation checker** for short uncited claims.
7. **find_similar_pages:** set recurring series aside; **list_pages:** add paging; **themes:** name a theme after its subject, not one page's title.
8. Add a competitor and a second scrape to John Hancock to test themes, monitoring, gaps and research for their real purpose.

## 10. How to run this test again

From the repo root (read-only; nothing is written to the app's data):

```
PYTHONPATH=. python docs/eval/john-hancock-2026-11-22/run_eval.py docs/eval/john-hancock-2026-11-22/results.json
python docs/eval/john-hancock-2026-11-22/tables.py
```

`cases.py` holds the 40 questions with their expected answers and pages; `reasons.py` the hand-checked reason for each result of this run (update it after a new run); `rag_eval_cases.csv` every question with the full answer, pages quoted, search rank and result.
