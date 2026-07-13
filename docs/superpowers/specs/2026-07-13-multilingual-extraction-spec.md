# Multilingual JD Extraction — Design Spec

> **Created:** 2026-07-13 · Deterministic-first (gazetteer/regex, no ML for extraction).
> Extends the English-only text extractors (yoe / degree / comp) to non-English JDs.
> Priority order from measured language mix: **German → French → Spanish** (then Dutch/Polish/Italian/PT).

## Why
~120k–155k of the ~874k Tier-3 detail backlog (14–18%) is **non-English**, concentrated in EU-boutique/SMB
sources (join ~99%, smartrecruiters ~38%, workable, recruitee, teamtailor, personio). Those JDs are now
captured (fetch_detail) but yield ~0 structured fields because `yoe.py`/`degree.py`/`comp.py` are English-only.
Enterprise giants (workday/oracle/icims/eightfold/radancy) are 80–100% English even abroad → **not** targets.
Top pools: **German ~55–64k** (join-driven), **French ~24k**, **Spanish ~10k**.

## Architecture (additive, English provably unchanged)
1. **`ExtractInput` gains `language: str = "en"`** (`src/ergon_tracker/extract/base.py`) — the single choke point
   every extractor reads. Default `"en"` keeps every existing call site (incl. the four `test_*_recall.py`) intact.
   `input_from_job` sets it from a detected language (below); until wired, everything stays `"en"`.
2. **Per-language vocab dicts.** In each extractor, rename the English vocab constants to `dict[str, X]` keyed by
   ISO code, with `["en"]` **byte-for-byte the current pattern**. Add `_vocab(lang, table, fallback="en") -> X`
   (returns the language entry or English). Thread `inp.language` from `extract()` into the private helpers,
   replacing implicit English references with `_vocab(inp.language, TABLE)`. Partial rollout is safe (missing
   language → English fallback → current behavior).
3. **`level.py` uses an optional kwarg** `infer_level(title, language="en")` (called directly in many places).
4. **`comp.py` needs the least**: `_parse_number` already disambiguates EU (`1.234,56`) vs US formats; currency
   is language-neutral. Only the *word* tables (`_CUE`, `_INTERVAL`, `_INTERVAL_BEFORE`, `_UP_TO`, `_FROM`,
   gross/net, abbrevs) need language keys. Add `R$`→BRL to `_SYMBOL_TO_CCY`. **Keep `_parse_number` as-is** but
   gate the EU-vs-US branch on currency for LatAm (MXN/COP `$30,000.00` is US-style, not EU).

## Language detection — stdlib stopword heuristic (NO new dependency)
No lang-detect lib is in the repo and the project is dependency-conscious. Use a small
`LANG_STOPWORDS: dict[str, frozenset[str]]` (~30–50 function words/lang: EN the/and/of, DE der/die/und/mit,
FR le/la/et/de, ES el/de/que/para, …). Tokenize the first ~500 chars of `description_text`, score overlap per
language, pick the max (English tiebreak/default). A wrong guess falls back to English = current behavior =
fails safe. Optionally bias by the posting's country/source first. Because vocab sets are near-disjoint across
languages and salary is currency+keyword anchored, a **try-all-languages, take-first-non-null** fallback is also
acceptable — detection just picks the order.

## Vocab — GERMAN (DE) [priority 1]
**YoE**: unit `Jahre(n)`; cue `Berufserfahrung|Praxiserfahrung|Erfahrung`; qualifiers `mindestens|mind.|min.|ab|
über|wenigstens` (→ at-least/from/over); ranges `N-M|N bis M`; compound `N-jährige`. Vague bands:
`erste (Berufs)Erfahrung`→(0,2), `mehrjährige`→(3,5), `fundierte`→(3,None), `langjährige`→(5,None).
**Degree→level**: Hauptschulabschluss/mittlere Reife/Realschulabschluss→highschool; Abitur/Fachabitur→highschool;
**Ausbildung/Berufsausbildung/Lehre→VOCATIONAL (false friend, NOT degree)**; Meister/Techniker→vocational;
Studium→degree(default bachelor); Bachelor(abschluss)/B.A./B.Sc./B.Eng.→bachelor; Hochschulabschluss/FH-Abschluss→
bachelor; Master(abschluss)/M.A./M.Sc./Magister→master; **Diplom/Dipl.-Ing.→master**; Promotion/Doktor/Dr.→phd
(Doktorand=candidate, not holder). Escape `oder vergleichbare Qualifikation`→soft.
**Salary**: nouns `Gehalt|Jahresgehalt|Monatsgehalt|Stundenlohn|Lohn|Vergütung|Entgelt|Einstiegsgehalt`; `brutto`
(≈always) / `netto` (usually calculator false-pos); intervals `pro Jahr|jährlich|p.a. | pro Monat|monatlich |
pro Stunde`. E.g. `46.000-59.000 EUR brutto pro Jahr`.

## Vocab — FRENCH (FR) [priority 2]
**YoE**: `N ans d'expérience`; `minimum N ans|N ans minimum|au moins N ans|à partir de N ans`; ranges `de N à M|
entre N et M`; verb-led `vous justifiez d'une expérience de N ans`. France Travail structured: `experienceLibelle`
="3 An(s)"/"24 Mois", `experienceExige`=E(required)/S(preferred)/D(débutant). Vague: `expérience significative`→
(1,3), `première expérience|jeune diplômé`→(0,1), `confirmé`→(4,None), `débutant accepté|sans expérience`→0.
**⚠ `Bac+N` is a DEGREE not YoE — never match `\d+ ans?` after `Bac+`/`niveau`.** Exclude `CDD/mission/stage de N mois`.
**Degree→level (RNCP/Bac+N)**: Bac→highschool; BTS/DUT/DEUG/Bac+2→associate; Licence/Licence pro/BUT/maîtrise/
Bac+3/Bac+4→bachelor; Master/diplôme d'ingénieur/grande école/DESS/DEA/MBA/Bac+5→master; Doctorat/HDR/Bac+8→phd;
CAP/BEP→vocational. Regex `Bac\s*\+\s*(\d{1,2})`. Strip `ou équivalent|minimum`.
**Salary**: `salaire|rémunération|fixe|package`; `brut`(std, usually annual K€)/`net`; intervals `brut annuel|par an|
/an | brut mensuel|par mois | sur 12/13 mois` (**13e mois = same annual, don't ×13/12**); **`TJM`=daily freelance
rate** (`450-700 € HT/jour`, HT=pre-VAT). Format: space-thousands `45 000 €`, `45K€`.

## Vocab — SPANISH (ES) [priority 3] (Spain + LatAm deltas)
**YoE**: cue `experiencia (laboral|profesional)|trayectoria|experiencia demostrable/comprobable/acreditada`; unit `años`;
`mínimo de N años|experiencia mínima de N años|al menos N años|más de N años|+N años`; ranges `entre N y M|de N a M`.
Vague: `amplia|dilatada experiencia`; negatives `sin experiencia|recién titulado/egresado`→0.
**⚠ age-range collision `entre 25 y 35 años` — gate on `experiencia`/`trayectoria` proximity; reject bare `N-M años`
near `edad`.**
**Degree→level (region-aware)**: ESO/Graduado Escolar→highschool; **Bachillerato/Bachiller (ES)→highschool** (NOT a
degree); FP/Grado Medio→vocational; Grado Superior/Técnico Superior/TSU→vocational; Diplomatura/Ingeniería Técnica→
associate/bachelor; **Licenciatura: ES-legacy→bachelor+(~4-5yr); MX/most LatAm→bachelor (current standard — don't
down-map)**; Grado universitario/Graduado→bachelor; **Grado de Bachiller/Bachiller (PE)→bachelor (NOT highschool)**;
Máster/Postgrado→master; Doctorado→phd. Modifiers `carrera afín|o similar` (flex), `se valorará` (preferred).
**Salary**: `salario|sueldo|retribución|remuneración|nómina`; `bruto`(default)/`neto`/`líquido`(CL); **`SBA`=Salario
Bruto Anual** (always gross-annual), `RBA` synonym; intervals `al año|anual|/año|bruto anual | al mes|mensual | por
hora|€/h`; **`14 pagas|12 pagas`** modifier (Spain often 14). K-shorthand `40-50K`=€/yr. Vague `salario competitivo|
según valía|a convenir`.

## Cross-cutting false-positive guards (ALL languages — highest-leverage precision safeguard)
- **Hours-not-salary (universal):** reject `\d{1,2}(,\d)?` immediately followed by an hours token:
  DE `Std.|Stunden|h/Woche`; ES `h/semana|horas semanales|jornada`; FR `h/semaine|heures/semaine`;
  PT `h/semana|horas semanais`; IT `ore settimanali|ore/settimana|h/settimana`; NL `uur per week|-urige werkweek|fte`.
  (This is the `38,5 h/Woche` → false salary `38.5` that join already exhibited.)
- **Salary positive-anchor:** accept a number only if a currency symbol / comp keyword / interval word is in a tight
  window; never a bare `NN,N`.
- **Not-base bonuses** (don't merge into salary): DE benefits; ES/PT `13º/14º`, `vakantiegeld` (NL, ~8%), `13e maand`,
  IT `tredicesima/quattordicesima/MBO/buoni pasto`; payment-count conventions (ES ×14, PT-PT ×14, FR 13e mois).
- **Degree-year vs YoE:** FR `Bac+N`, IT `-ennale`/`laurea triennale`, PT `12º ano`, ES age ranges.

## PT / IT / NL (priority 2 — full tables in the research transcript, summary)
- **IT:** `RAL`=gross-annual (dominant); `anni di esperienza`, `almeno/minimo N anni`, `-ennale` (biennale≈2…decennale≈10,
  disambiguate `esperienza` vs `laurea`); Laurea Triennale→bachelor, Magistrale/Specialistica→master, **Master I/II
  livello = professional cert NOT academic**, Dottorato→phd.
- **PT:** `N anos de experiência`; Ensino Secundário(PT)/Médio(BR)→highschool, Licenciatura→bachelor, Bacharelado(BR)→
  bachelor, Mestrado→master, Doutoramento(PT)/Doutorado(BR)→phd; € suffix (PT) / R$ prefix (BR); ×14 (PT) / 13º (BR).
- **NL:** `minimaal N jaar (werk)ervaring`, Junior/Medior/Senior banding; VMBO/HAVO/VWO→highschool, MBO→vocational,
  **HBO/WO bachelor→bachelor (retain subtag)**, master→master, promotie→phd; **`werk- en denkniveau`=experience-
  accepted (weaker than `afgeronde opleiding`)**; `bruto ... per jaar/maand`, `€ 45.000,-` (`,-`=whole euros),
  exclude `vakantiegeld` 8%, `fte`. BE deltas: `eindejaarspremie`, professionele/academische bachelor.

## Benchmark (mirror the existing recall harness, keep English gates as regression guards)
Existing: `tests/test_{yoe,degree,comp,level}_recall.py` read `tests/fixtures/{yoe,degree,salary,level}_corpus.jsonl`
(`{"text","expect","src"}`), gates ratcheted below measured English recall/precision, blind-labeled.
Plan: (1) add a `lang` field to corpus records; (2) build a small multilingual corpus per language — bootstrap from
**France Travail Offres API** (self-labeled `experienceLibelle`/`experienceExige`/`salaire.commentaire`/degree tags,
FR) and **aida-ugent/JobHop** (NL/BE, 200 hand-annotated), plus blind-labeled samples of our own join/SR JDs;
(3) **keep the English gate byte-identical** (`lang=="en"` regression guard) and add **separate per-language gates**
(`RECALL_GATE_DE`, …) ratcheted independently; (4) per-language `test_corpus_is_substantial` min-count.

## Build order
1. `ExtractInput.language` + `LANG_STOPWORDS` detector + `_vocab` helper + wire `input_from_job`/`enrich_in_place`.
2. German vocab across yoe/degree/comp (biggest pool) + DE benchmark corpus + gates.
3. French (Bac+N degree system is the fiddly bit) + corpus + gates.
4. Spanish + corpus + gates.
5. (later) IT / PT / NL.
Each language ships with: vocab tables, a blind-labeled corpus slice, per-language gates, and a proof the English
gates are unchanged (regression guard).
