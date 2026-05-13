# Ceraldi Group ERP — Claude Code Context

## Project Context
- ERP/gestionale italiano per Ceraldi Group, deploy su Render (gestionale-ceraldi.onrender.com)
- Backend: Python + FastAPI + Motor (async MongoDB Atlas), database "Gestionale"
- Frontend: single-page app in `static/index.html`
- Fatture elettroniche: formato FatturaPA XML (standard AdE)
- Corrispettivi: formato COR10 XML (`DatiCorrispettivi`) da registratore telematico
- Metodo pagamento: SEMPRE dall'anagrafica fornitore (`fornitori` collection), MAI dall'XML
- Collections chiave: `invoices`, `fornitori`, `prima_nota_cassa`, `prima_nota_banca`, `prima_nota_provvisori`, `estratto_conto`, `corrispettivi`, `f24_commercialista`

## Bug Fixing Approach
- SEMPRE trovare e fixare la root cause, non i sintomi. Traccia il flusso dati completo: parser → MongoDB → API → frontend prima di toccare il codice.
- I fix devono essere automatici e trasparenti — mai aggiungere bottoni "Fix dati", pagine admin di repair, o script di migrazione da cliccare manualmente.
- Se esistono dati corrotti da un bug precedente, scrivere uno script one-shot da eseguire da terminale, NON esporre come feature UI permanente.
- Non aprire PR finché non hai identificato la singola root cause.

## Pre-Merge Verification
Prima di ogni PR verificare:
1. Ogni funzione chiamata è definita nel file (niente NameError/ReferenceError)
2. Ogni query MongoDB su `_id` ha gestione ObjectId/str fallback
3. I nomi dei field sono coerenti tra backend Python e frontend JS (es. `stato` vs `status`, nomi collection)
4. Eseguire `pyflakes` su ogni file Python modificato e correggere tutti gli errori prima di fare push

## PR Workflow
- Claude crea E merga sempre i PR — l'utente non deve farlo mai
- Sviluppare su branch dedicato, mai pushare direttamente su `main`
- Dopo il merge attendere il deploy Render prima di dichiarare il task completato
