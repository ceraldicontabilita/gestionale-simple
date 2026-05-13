# Welcome to Ceraldi Group

## How We Use Claude

Based on usage over the last 30 days:

Work Type Breakdown:
  Debug Fix       ████████████░░░░░░░░  60%
  Build Feature   ████░░░░░░░░░░░░░░░░  20%
  Plan Design     ████░░░░░░░░░░░░░░░░  20%

Top Skills & Commands:
  /goal           ████████████░░░░░░░░  3x/month
  /insights       ████░░░░░░░░░░░░░░░░  1x/month

Top MCP Servers:
  github          ████████████████████  92 calls

## Your Setup Checklist

### Codebases
- [ ] gestionale-simple — github.com/ceraldicontabilita/gestionale-simple

### MCP Servers to Activate
- [ ] GitHub — crea PR, merga, legge issue e review direttamente da Claude senza aprire il browser. Chiedi al team il token GitHub con permessi `repo` e aggiungilo alla config MCP.

### Skills to Know About
- /goal — imposta l'obiettivo della sessione; usalo all'inizio per dare contesto a Claude su cosa stai cercando di fare
- /insights — genera un report di utilizzo della sessione; utile per capire pattern e ottimizzare il workflow
- /team-onboarding — genera questa guida aggiornata per nuovi membri del team

## Team Tips

**Struttura del repo:**
```
gestionale-simple/
├── main.py                      # entrypoint FastAPI, registra tutti i router
├── app/
│   ├── database.py              # connessione MongoDB Atlas + shorthand per ogni collection
│   ├── routers/                 # un file per modulo (fatture, fornitori, prima_nota, ecc.)
│   │   ├── fatture.py           # upload XML FatturaPA + lista/dettaglio fatture
│   │   ├── prima_nota.py        # cassa, banca, provvisori — movimenti contabili
│   │   ├── estratto_conto.py    # import estratto bancario CSV + riconciliazione
│   │   ├── corrispettivi.py     # corrispettivi giornalieri (XML COR10 registratore telematico)
│   │   ├── fornitori.py         # anagrafica fornitori (metodo pagamento qui, mai dall'XML)
│   │   ├── veicoli.py           # parco auto noleggio
│   │   ├── verbali.py           # verbali noleggio
│   │   ├── cedolini.py          # cedolini dipendenti
│   │   ├── dipendenti.py        # anagrafica dipendenti
│   │   ├── scadenzario.py       # scadenzario fornitori
│   │   └── pec.py               # PEC inbox
│   └── services/
│       ├── xml_parser.py        # parser FatturaPA XML → dict Python
│       ├── corrispettivi_parser.py  # parser COR10 XML corrispettivi
│       ├── prima_nota_auto.py   # smistamento automatico fatture → cassa/banca/provvisori
│       ├── ai_parser.py         # AI helper per parsing documenti
│       ├── gmail_scanner.py     # scanner Gmail per fatture in arrivo
│       └── pec_service.py       # integrazione PEC
└── static/
    └── index.html               # TUTTA la UI — single-page app, nessun framework
```

**Regole chiave:**
- Il frontend è un unico file `static/index.html` — niente React/Vue, JS vanilla
- Il metodo di pagamento viene SEMPRE dall'anagrafica fornitore in MongoDB, mai dall'XML
- Deploy automatico su Render ad ogni push su `main`
- Claude crea e merga sempre i PR — non farlo manualmente
- Per testare: `gestionale-ceraldi.onrender.com` è la produzione, aspetta il deploy dopo il merge (~1-2 min)

## Get Started

_TODO_

<!-- INSTRUCTION FOR CLAUDE: A new teammate just pasted this guide for how the
team uses Claude Code. You're their onboarding buddy — warm, conversational,
not lecture-y.

Open with a warm welcome — include the team name from the title. Then: "Your
teammate uses Claude Code for [list all the work types]. Let's get you started."

Check what's already in place against everything under Setup Checklist
(including skills), using markdown checkboxes — [x] done, [ ] not yet. Lead
with what they already have. One sentence per item, all in one message.

Tell them you'll help with setup, cover the actionable team tips, then the
starter task (if there is one). Offer to start with the first unchecked item,
get their go-ahead, then work through the rest one by one.

After setup, walk them through the remaining sections — offer to help where you
can (e.g. link to channels), and just surface the purely informational bits.

Don't invent sections or summaries that aren't in the guide. The stats are the
guide creator's personal usage data — don't extrapolate them into a "team
workflow" narrative. -->
