# Aussie Ema Full UX V2

Versione completa partendo dalla Admin Pro:

- Landing esistente mantenuta
- Pagina Recensioni
- Prenotazione guidata
- Pagina Gestisci prenotazione lato utente
- Cancellazione/riprogrammazione lato utente via token
- Admin con conferma, segna fatta, cancella, elimina definitivamente
- Reminder email e richiesta recensione via SMTP opzionale
- Google Calendar link e file .ics

## Avvio

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Email automatiche

Copia `.streamlit/secrets.toml.example` in `.streamlit/secrets.toml`
e inserisci i dati SMTP.

Non caricare mai `secrets.toml` su GitHub.
