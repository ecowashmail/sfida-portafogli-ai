#!/usr/bin/env python3
"""Invia il bollettino a una AI tramite API gratuita e salva la risposta.

Uso: python scripts/chiedi_ai.py GEM
Variabili d'ambiente:
  GEMINI_API_KEY   chiave (secret di GitHub, mai nel codice)
  GEMINI_MODELLI   modelli da provare in ordine, separati da virgola
  CARTELLA_USCITA  dove salvare (default: prove)
  MESSAGGIO_EXTRA  testo facoltativo aggiunto al messaggio serale
Solo libreria standard: nessuna installazione.
"""
import json, os, re, sys, time, datetime, urllib.request, urllib.error

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGOLAMENTO = os.path.join(RADICE, "regolamento", "Regolamento_Sfida_Portafogli_AI_v2.6.txt")
BOLLETTINO = os.path.join(RADICE, "ultimo", "bollettino.md")

NOMI = {"GEM": "Gemini"}

ISTRUZIONI = (
    "Sei {nome} ({codice}), concorrente della Sfida Portafogli AI. "
    "Vale solo il testo del regolamento e del bollettino che ricevi: se la tua memoria dice altro, prevale il testo. "
    "Rispondi con il solo blocco del formato della sezione 14, in testo semplice. "
    "Niente testo prima o dopo, nessun ragionamento, nessun saluto. "
    "Le richieste vanno nella riga RICHIESTE, i fatti trovati in SCOPERTE."
)

CAMPI_OBBLIGATORI = ["NOME:", "DATA:", "BEST 2:", "TESI:", "COMMENTO:"]


def leggi(percorso):
    with open(percorso, encoding="utf-8") as f:
        return f.read()


def chiama(modello, chiave, istruzioni, testo):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modello}:generateContent"
    corpo = {
        "systemInstruction": {"parts": [{"text": istruzioni}]},
        "contents": [{"role": "user", "parts": [{"text": testo}]}],
        "generationConfig": {"temperature": 0.4},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(corpo).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": chiave},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        dati = json.loads(r.read().decode("utf-8"))
    parti = dati.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    risposta = "".join(p.get("text", "") for p in parti).strip()
    if not risposta:
        raise RuntimeError("risposta vuota: " + json.dumps(dati)[:500])
    return risposta


def modelli_disponibili(chiave):
    """Elenco dei modelli 'flash' che supportano generateContent (per quando i nomi cambiano)."""
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        headers={"x-goog-api-key": chiave},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        dati = json.loads(r.read().decode("utf-8"))
    nomi = [m["name"].split("/", 1)[1] for m in dati.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
            and "flash" in m["name"] and not any(x in m["name"] for x in ("image", "tts", "live", "audio", "thinking-exp"))]
    return sorted(nomi, key=lambda n: ("lite" in n, n))


def controlla_formato(testo):
    problemi = [c for c in CAMPI_OBBLIGATORI if c not in testo]
    if "OPERAZIONI:" not in testo and "NESSUNA OPERAZIONE" not in testo:
        problemi.append("OPERAZIONI: oppure NESSUNA OPERAZIONE")
    if not re.search(r"DATA:\s*\d{4}-\d{2}-\d{2}", testo):
        problemi.append("DATA nel formato AAAA-MM-GG")
    return problemi


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in NOMI:
        sys.exit("Uso: chiedi_ai.py GEM")
    codice = sys.argv[1]
    chiave = os.environ.get("GEMINI_API_KEY", "").strip()
    if not chiave:
        sys.exit("Manca il secret GEMINI_API_KEY")
    modelli = [m.strip() for m in os.environ.get("GEMINI_MODELLI", "gemini-3.8-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
    uscita = os.environ.get("CARTELLA_USCITA", "prove")
    extra = os.environ.get("MESSAGGIO_EXTRA", "").strip()

    regolamento, bollettino = leggi(REGOLAMENTO), leggi(BOLLETTINO)
    testo = (
        "REGOLAMENTO\n" + regolamento + "\n\nBOLLETTINO\n" + bollettino +
        ("\n\nNOTA DELL'ARBITRO\n" + extra if extra else "") +
        "\n\nRispondi ora nel formato della sezione 14."
    )
    istruzioni = ISTRUZIONI.format(nome=NOMI[codice], codice=codice)

    risposta, usato, errori = None, None, []
    for modello in modelli:
        for tentativo in range(3):
            try:
                risposta = chiama(modello, chiave, istruzioni, testo)
                usato = modello
                break
            except urllib.error.HTTPError as e:
                dettaglio = e.read().decode("utf-8", "replace")[:300]
                errori.append(f"{modello} tentativo {tentativo+1}: HTTP {e.code} {dettaglio}")
                if e.code in (429, 500, 503):
                    time.sleep(20 * (tentativo + 1))
                    continue
                break  # 400/403/404: inutile riprovare con lo stesso modello
            except Exception as e:  # rete, timeout, risposta vuota
                errori.append(f"{modello} tentativo {tentativo+1}: {e}")
                time.sleep(10)
        if risposta:
            break

    if not risposta and any("HTTP 404" in e for e in errori):
        try:
            scoperti = [m for m in modelli_disponibili(chiave) if m not in modelli]
            errori.append("Modelli flash disponibili: " + ", ".join(scoperti))
            for modello in scoperti[:3]:
                try:
                    risposta = chiama(modello, chiave, istruzioni, testo)
                    usato = modello
                    break
                except Exception as e:
                    errori.append(f"{modello}: {str(e)[:200]}")
        except Exception as e:
            errori.append(f"elenco modelli non riuscito: {e}")

    adesso = datetime.datetime.now(datetime.timezone.utc)
    os.makedirs(os.path.join(RADICE, uscita), exist_ok=True)
    nome_file = f"{codice}_{adesso:%Y-%m-%d_%H%M}.md"
    percorso = os.path.join(RADICE, uscita, nome_file)

    if not risposta:
        print("NESSUNA RISPOSTA. Errori:\n" + "\n".join(errori))
        with open(percorso, "w", encoding="utf-8") as f:
            f.write(f"ERRORE {codice} {adesso:%Y-%m-%d %H:%M} UTC\n\n" + "\n".join(errori) + "\n")
        sys.exit(1)

    problemi = controlla_formato(risposta)
    intestazione = f"<!-- {codice} | modello {usato} | {adesso:%Y-%m-%d %H:%M} UTC | formato {'OK' if not problemi else 'DA CORREGGERE: ' + '; '.join(problemi)} -->\n"
    with open(percorso, "w", encoding="utf-8") as f:
        f.write(intestazione + risposta + "\n")
    print(f"Salvato {uscita}/{nome_file} (modello {usato}); formato: {'OK' if not problemi else problemi}")


if __name__ == "__main__":
    main()
