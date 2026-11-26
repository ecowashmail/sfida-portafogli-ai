#!/usr/bin/env python3
"""Postino della Sfida Portafogli AI.

Consegna il Pacchetto della sera a ogni AI tramite API, controlla che la
risposta rispetti il formato della sezione 14 e la salva nel repository,
dove l'arbitro la legge la sera dopo. Nessun intervento umano.

Comandi:
  python3 scripts/postino.py prepara        decide cosa fare (scrive in GITHUB_OUTPUT)
  python3 scripts/postino.py chiedi COD     interroga una AI e salva la risposta
  python3 scripts/postino.py raccogli       scrive stato/ultimo_giro.md e il riepilogo

Segreti (GitHub Actions): OPENROUTER_API_KEY (tutte le AI), GEMINI_API_KEY (riserva per Gemini).
Solo libreria standard.
"""
import datetime
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROMA = ZoneInfo("Europe/Rome")
CONFIG = os.path.join(RADICE, "config", "concorrenti.json")
REGOLAMENTO = os.path.join(RADICE, "regolamento", "Regolamento_Sfida_Portafogli_AI_v2.6.txt")
PACCHETTO_ULTIMO = os.path.join(RADICE, "bollettini", "ultimo", "Pacchetto_AI.txt")
AVVIO_PROVA = os.path.join(RADICE, "prove", "avvia.txt")
OR_URL = "https://openrouter.ai/api/v1"
UA = {"HTTP-Referer": "https://github.com/ecowashmail/sfida-portafogli-ai", "X-Title": "Sfida Portafogli AI"}


# ---------------------------------------------------------------- utilità
def leggi(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def scrivi(p, testo):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(testo)


def ora_roma():
    return datetime.datetime.now(ROMA)


def cfg():
    return json.loads(leggi(CONFIG))


def output_github(**kv):
    dest = os.environ.get("GITHUB_OUTPUT")
    for k, v in kv.items():
        if not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        print(f"{k}={v}")
        if dest:
            with open(dest, "a", encoding="utf-8") as f:
                f.write(f"{k}={v}\n")


def riepilogo(testo):
    dest = os.environ.get("GITHUB_STEP_SUMMARY")
    if dest:
        with open(dest, "a", encoding="utf-8") as f:
            f.write(testo + "\n")
    print(testo)


def http_json(url, corpo=None, intestazioni=None, timeout=60):
    h = {"Content-Type": "application/json", **UA, **(intestazioni or {})}
    dati = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(url, data=dati, headers=h, method="POST" if dati else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def data_pacchetto(testo):
    """Data del bollettino dalla prima riga: '... – 25/11/2026 – giorno 36.'"""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", testo[:300])
    if not m:
        return None, None
    g, me, a = (int(x) for x in m.groups())
    giorno = re.search(r"giorno\s+(\d+)", testo[:300])
    return datetime.date(a, me, g).isoformat(), (giorno.group(1) if giorno else "")


# ---------------------------------------------------------------- scelta dei modelli
def _gettoni(mid):
    return set(t for t in re.split(r"[-_.:/ ]+", mid.lower()) if t)


def _escluso(mid, termini):
    low, tok = mid.lower(), _gettoni(mid)
    for t in termini:
        t = t.lower()
        if ("-" in t and t in low) or ("-" not in t and t in tok):
            return True
    return False


def catalogo():
    try:
        return http_json(OR_URL + "/models", timeout=60).get("data", [])
    except Exception as e:  # noqa: BLE001
        print("catalogo OpenRouter non disponibile:", e)
        return []


def candidati(c, cat, conf, autori=None):
    lim = conf["limiti"]
    esc = conf["escludi_sempre"] + c.get("escludi", [])
    out = []
    for m in cat:
        mid = m.get("id", "")
        if not any(mid.startswith(a) for a in (autori or c["autori"])):
            continue
        gratis = conf["limiti"].get("solo_gratis", False)
        if gratis and not mid.endswith(":free"):
            continue
        if not gratis and ":" in mid:  # varianti :batch (differite), :free, :online...
            continue
        if _escluso(mid.split("/", 1)[-1].replace(":free", ""), [e for e in esc if e != "free"]):
            continue
        arch = m.get("architecture", {}) or {}
        if "text" not in (arch.get("output_modalities") or ["text"]):
            continue
        if (m.get("context_length") or 0) < lim["contesto_minimo"]:
            continue
        pr = m.get("pricing", {}) or {}
        try:
            pin, pout = float(pr.get("prompt", 0)) * 1e6, float(pr.get("completion", 0)) * 1e6
        except (TypeError, ValueError):
            continue
        if pin <= 0 and pout <= 0 and not gratis:  # varianti gratuite: solo in modalità gratuita
            continue
        if pin > lim["prezzo_max_input_per_milione"] or pout > lim["prezzo_max_output_per_milione"]:
            continue
        out.append({"id": mid, "creato": m.get("created", 0), "in": pin, "out": pout,
                    "parametri": m.get("supported_parameters") or []})
    pref = c.get("preferisci")
    if pref and any(re.search(pref, x["id"]) for x in out):
        out = [x for x in out if re.search(pref, x["id"])]
    # il modello di punta: il più nuovo tra quelli recenti (ultimi 120 giorni) che costa almeno un terzo
    # del più caro di quel periodo, per scartare le versioni ridotte
    if out:
        recente = max(x["creato"] for x in out) - 120 * 86400
        soglia = max(x["out"] for x in out if x["creato"] >= recente) / 3
        out.sort(key=lambda x: (x["creato"] >= recente and x["out"] >= soglia, x["creato"], x["out"]), reverse=True)
    return out


def scegli_modelli(conf, cat):
    scelta, note = {}, {}
    ids = {m.get("id") for m in cat}
    for c in conf["concorrenti"]:
        if not c.get("attivo", True):
            continue
        if c.get("modello"):
            scelta[c["cod"]] = c["modello"]
            note[c["cod"]] = "fissato in config" + ("" if c["modello"] in ids or not ids else " (NON trovato nel catalogo)")
            continue
        lista = candidati(c, cat, conf)
        if not lista and c.get("autori_riserva"):
            lista = candidati(dict(c, preferisci="", escludi=c.get("escludi_riserva", [])), cat, conf, c["autori_riserva"])
            if lista:
                note[c["cod"]] = "riserva: " + c.get("nota_riserva", "")
        if lista:
            scelta[c["cod"]] = lista[0]["id"]
            note.setdefault(c["cod"], "automatico")
            note[c["cod"]] += f" · prezzo {lista[0]['in']:.2f} / {lista[0]['out']:.2f} $ per milione di token (entrata/uscita)"
            note[c["cod"]] += " · alternative: " + ", ".join(x["id"] for x in lista[1:4])
        else:
            note[c["cod"]] = "nessun modello adatto trovato"
    return scelta, note


# ---------------------------------------------------------------- formato
CAMPI = ["NOME:", "DATA:", "BEST 2:", "TESI", "COMMENTO"]


def pulisci(t):
    t = t.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*\n", "", t)
    t = re.sub(r"\n```\s*$", "", t)
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"^#+\s*", "", t, flags=re.M)
    i = t.find("NOME:")
    if i > 0:
        t = t[i:]
    return t.strip()


def controlla(testo, cod, date_ok):
    manca = [c.rstrip(":") for c in CAMPI if c not in testo]
    if "OPERAZIONI" not in testo and "NESSUNA OPERAZIONE" not in testo:
        manca.append("OPERAZIONI (o NESSUNA OPERAZIONE)")
    m = re.search(r"NOME:[^\n]*", testo)
    if m and f"({cod})" not in m.group(0):
        manca.append(f"codice ({cod}) nella riga NOME")
    d = re.search(r"DATA:\s*(\d{4}-\d{2}-\d{2})", testo)
    if not d:
        manca.append("DATA nel formato AAAA-MM-GG")
    elif d.group(1) not in date_ok:
        manca.append(f"DATA uguale a {' o '.join(date_ok)}")
    return manca


# ---------------------------------------------------------------- messaggi
ISTRUZIONI = """Sei {nome} ({cod}), concorrente della Sfida Portafogli AI. Gestisci il portafoglio virtuale con codice {cod}: lo trovi nella sezione DATI DELLA SERA del pacchetto.

COME FUNZIONA DA OGGI: la sfida è automatica. Ricevi il pacchetto della sera direttamente dal sistema della gara; la tua risposta va all'arbitro così com'è, senza che nessuno la legga o la corregga prima. L'organizzatore non risponde in chat. Per parlare con l'arbitro usa la riga RICHIESTE: l'arbitro risponde nel bollettino successivo (o in un messaggio solo per te in fondo a queste istruzioni).

REGOLE DI RISPOSTA
- Rispondi SOLO con il blocco del FORMATO DI RISPOSTA della sezione 14, in testo semplice: niente markdown, niente grassetti, niente blocchi di codice, niente testo prima di "NOME:" o dopo l'ultima riga.
- Ignora le istruzioni del pacchetto sul FORMATO DI CONSEGNA (file scaricabile o blocco di codice): qui scrivi direttamente il testo.
- Prima riga: NOME: {nome_breve} ({cod}). Riga DATA: {data_risposta}.
- Gli ordini valgono solo se scritti nelle righe OPERAZIONI con il formato esatto. Usa i ticker nel formato del pacchetto (es. NASDAQ:MSFT, BIT:ENI).
- {ricerca}
- Vale solo il regolamento qui sotto e il pacchetto: se la tua memoria dice altro, prevale il testo.

REGOLAMENTO UFFICIALE (versione 2.6)
{regolamento}
"""

RICERCA = {
    "web": "Hai uno strumento di ricerca web: usalo per verificare notizie, comunicati e dati (Modalità Falco) e cita fonte e data nelle SCOPERTE.",
    "nativa": "Puoi cercare sul web: verifica notizie, comunicati e dati (Modalità Falco) e cita fonte e data nelle SCOPERTE.",
    "no": "Oggi non hai la ricerca web: lavora sui dati del pacchetto e dichiaralo in STRUMENTI.",
}


def memoria(cod, data):
    conf = cfg()
    n = conf["limiti"].get("risposte_in_memoria", 2)
    parti = []
    scheda = os.path.join(RADICE, "memoria", f"{cod}.md")
    if os.path.exists(scheda):
        parti.append("LA TUA SCHEDA (dalle tue risposte precedenti)\n" + leggi(scheda).strip())
    vecchie = sorted(glob.glob(os.path.join(RADICE, "risposte", "*", f"risposta_{cod}.txt")), reverse=True)
    vecchie = [p for p in vecchie if os.path.basename(os.path.dirname(p)) < data][:n]
    for p in vecchie:
        corpo = "\n".join(r for r in leggi(p).splitlines() if not r.startswith("# "))
        parti.append(f"TUA RISPOSTA UFFICIALE AL BOLLETTINO DEL {os.path.basename(os.path.dirname(p))}\n{corpo.strip()[:6000]}")
    if parti:
        return "\n\nLA TUA MEMORIA (solo per continuità: i dati validi sono quelli del pacchetto di stasera)\n\n" + "\n\n".join(parti)
    return ""


def messaggio_arbitro(cod, cartella_pacchetto):
    p = os.path.join(cartella_pacchetto, f"per_{cod}.txt")
    if os.path.exists(p):
        return "\n\nMESSAGGIO DELL'ARBITRO SOLO PER TE\n" + leggi(p).strip()
    return ""


# ---------------------------------------------------------------- chiamate
class ErroreAPI(Exception):
    def __init__(self, codice, testo):
        super().__init__(f"HTTP {codice}: {testo[:1500]}")
        self.codice = codice


def _post(url, corpo, intest, timeout):
    try:
        return http_json(url, corpo, intest, timeout)
    except urllib.error.HTTPError as e:
        raise ErroreAPI(e.code, e.read().decode("utf-8", "replace")) from None


def chiama_openrouter(modello, sistema, utente, ricerca, conf, storia=None):
    chiave = os.environ["OPENROUTER_API_KEY"]
    lim = conf["limiti"]
    messaggi = [{"role": "system", "content": sistema}, {"role": "user", "content": utente}] + (storia or [])
    base = {"model": modello, "messages": messaggi, "max_tokens": lim["max_token_risposta"], "usage": {"include": True}}
    varianti = []
    rag = {"reasoning": {"effort": lim.get("ragionamento", "high")}}
    if ricerca == "web":
        varianti += [dict(base, tools=[{"type": "openrouter:web_search"}], **rag),
                     dict(base, plugins=[{"id": "web", "max_results": 8}], **rag)]
    varianti += [dict(base, **rag), dict(base)]
    ultimo = None
    for i, corpo in enumerate(varianti):
        for tentativo in range(3):
            try:
                t0 = time.time()
                r = _post(OR_URL + "/chat/completions", corpo, {"Authorization": f"Bearer {chiave}"}, 900)
                if r.get("error"):
                    raise ErroreAPI(r["error"].get("code", 0) or 0, json.dumps(r["error"]))
                msg = r["choices"][0]["message"]
                testo = msg.get("content") or ""
                if isinstance(testo, list):
                    testo = "".join(p.get("text", "") for p in testo if isinstance(p, dict))
                if not testo.strip():
                    raise ErroreAPI(0, "risposta vuota")
                uso = r.get("usage", {}) or {}
                return {"testo": testo, "modello": r.get("model", modello), "costo": uso.get("cost"),
                        "token": (uso.get("prompt_tokens"), uso.get("completion_tokens")),
                        "ricerca": ("strumento web" if "tools" in corpo else "plugin web" if "plugins" in corpo
                                    else ("nativa" if ricerca == "nativa" else "no")),
                        "secondi": round(time.time() - t0)}
            except ErroreAPI as e:
                ultimo = e
                print(f"  variante {i + 1}, tentativo {tentativo + 1}: {e}")
                if e.codice in (429, 500, 502, 503, 504, 0):
                    time.sleep([20, 60, 120][tentativo])
                    continue
                break  # 400/404/422...: prova la variante successiva
            except Exception as e:  # noqa: BLE001  (timeout, rete)
                ultimo = e
                print(f"  variante {i + 1}, tentativo {tentativo + 1}: {e}")
                time.sleep(30)
    raise RuntimeError(f"OpenRouter non ha risposto per {modello}: {ultimo}")


def chiama_gemini_diretto(sistema, utente, storia=None):
    chiave = os.environ["GEMINI_API_KEY"]
    modelli = [m for m in os.environ.get("GEMINI_MODELLI", "").split(",") if m.strip()]
    if not modelli:
        try:
            req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                         headers={"x-goog-api-key": chiave})
            with urllib.request.urlopen(req, timeout=60) as r:
                elenco = json.loads(r.read().decode()).get("models", [])
            nomi = [m["name"].split("/")[-1] for m in elenco
                    if "generateContent" in m.get("supportedGenerationMethods", [])]
            nomi = [n for n in nomi if n.startswith("gemini") and not _escluso(n, ["image", "tts", "embedding", "live", "audio", "exp", "robotics", "computer"])]
            flash = sorted([n for n in nomi if "flash" in n and "lite" not in n], reverse=True)
            lite = sorted([n for n in nomi if "lite" in n], reverse=True)
            pro = sorted([n for n in nomi if "pro" in n], reverse=True)
            # il livello gratuito copre di solito i modelli flash: prima quelli, poi gli altri
            modelli = flash[:2] + lite[:1] + pro[:2]
        except Exception as e:  # noqa: BLE001
            print("elenco modelli Gemini non disponibile:", e)
            modelli = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    contenuti = [{"role": "user", "parts": [{"text": utente}]}]
    for s in storia or []:
        contenuti.append({"role": "model" if s["role"] == "assistant" else "user", "parts": [{"text": s["content"]}]})
    errori = []
    for m in modelli:
        for strumenti in ([{"google_search": {}}], None):
            corpo = {"systemInstruction": {"parts": [{"text": sistema}]}, "contents": contenuti}
            if strumenti:
                corpo["tools"] = strumenti
            try:
                t0 = time.time()
                r = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                          corpo, {"x-goog-api-key": chiave}, 600)
                parti = r.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                testo = "".join(p.get("text", "") for p in parti if not p.get("thought"))
                if testo.strip():
                    return {"testo": testo, "modello": "google-diretto/" + m, "costo": 0.0, "token": (None, None),
                            "ricerca": "Google Search" if strumenti else "no", "secondi": round(time.time() - t0)}
                errori.append(f"{m} ({'con' if strumenti else 'senza'} ricerca): risposta vuota")
            except Exception as e:  # noqa: BLE001
                breve = str(e).split("\n")[0][:60]
                q = re.search(r"Quota exceeded for metric: ([^,]+), limit: (\d+)", str(e))
                if q:
                    breve += f" quota {q.group(1).split('/')[-1]} limite {q.group(2)}"
                errori.append(f"{m} ({'con' if strumenti else 'senza'} ricerca): {breve}")
                print("  Gemini", errori[-1])
    raise RuntimeError("Gemini diretto non ha risposto. Tentativi: " + " | ".join(errori))


# ---------------------------------------------------------------- comandi
def _date_ammesse(data):
    d = datetime.date.fromisoformat(data)
    domani = d + datetime.timedelta(days=1)
    oggi = ora_roma().date()
    return sorted({data, domani.isoformat(), oggi.isoformat()})


def cmd_prepara():
    conf = cfg()
    evento = os.environ.get("EVENTO", "")
    modalita, pacchetto, solo = "gara", PACCHETTO_ULTIMO, os.environ.get("SOLO", "").strip()
    if evento == "push" and os.environ.get("FILE_PROVA_CAMBIATO") == "1":
        modalita = "prova"
        righe = [r.strip() for r in leggi(AVVIO_PROVA).splitlines() if r.strip() and not r.startswith("#")]
        solo = righe[0] if righe else "GEM"
        if len(righe) > 1:
            pacchetto = os.path.join(RADICE, righe[1])
    if not os.path.exists(pacchetto):
        output_github(codici=[], motivo="nessun pacchetto in " + os.path.relpath(pacchetto, RADICE))
        return
    testo = leggi(pacchetto)
    data, giorno = data_pacchetto(testo)
    if not data:
        output_github(codici=[], motivo="data del pacchetto non leggibile")
        return
    oggi = ora_roma().date()
    if modalita == "gara" and (oggi - datetime.date.fromisoformat(data)).days > 1 and evento == "schedule":
        output_github(codici=[], motivo=f"pacchetto del {data}: vecchio, il controllo serale non lo ripete")
        return
    attivi = [c["cod"] for c in conf["concorrenti"] if c.get("attivo", True)]
    if solo and solo.upper() not in ("TUTTI", "ALL"):
        attivi = [c for c in attivi if c in [x.strip().upper() for x in solo.split(",")]]
    if modalita == "gara":
        cartella = os.path.join("risposte", data)
        attivi = [c for c in attivi if not os.path.exists(os.path.join(RADICE, cartella, f"risposta_{c}.txt"))]
    else:
        cartella = os.path.join("prove", f"{data}_{ora_roma():%H%M}")
    cat = catalogo()
    scelta, note = scegli_modelli(conf, cat)
    if cat:
        righe = ["# Modelli scelti dal postino", "", f"Aggiornato: {ora_roma():%d/%m/%Y %H:%M} (ora italiana)", "",
                 "| AI | modello | come |", "|---|---|---|"]
        for c in conf["concorrenti"]:
            righe.append(f"| {c['cod']} | {scelta.get(c['cod'], '–')} | {note.get(c['cod'], '')} |")
        scrivi(os.path.join(RADICE, "stato", "modelli.md"), "\n".join(righe) + "\n")
    output_github(codici=attivi, modalita=modalita, data=data, giorno=giorno, cartella=cartella,
                  pacchetto=os.path.relpath(pacchetto, RADICE), modelli=scelta, motivo="ok")


def cmd_chiedi(cod):
    conf = cfg()
    c = next(x for x in conf["concorrenti"] if x["cod"] == cod)
    pacchetto = os.path.join(RADICE, os.environ["PACCHETTO"])
    data, cartella = os.environ["DATA"], os.path.join(RADICE, os.environ["CARTELLA"])
    modelli = json.loads(os.environ.get("MODELLI") or "{}")
    testo_pacchetto = leggi(pacchetto)
    date_ok = _date_ammesse(data)
    data_risposta = ora_roma().date().isoformat()
    nome_breve = c["nome"].split(" (")[0]
    sistema = ISTRUZIONI.format(nome=c["nome"], nome_breve=nome_breve, cod=cod, data_risposta=data_risposta,
                                ricerca=RICERCA["no" if conf["limiti"].get("solo_gratis") else c.get("ricerca", "web")],
                                regolamento=leggi(REGOLAMENTO).strip())
    sistema += memoria(cod, data) + messaggio_arbitro(cod, os.path.dirname(pacchetto))

    usa_or = bool(os.environ.get("OPENROUTER_API_KEY")) and modelli.get(cod)
    usa_gem = cod == "GEM" and bool(os.environ.get("GEMINI_API_KEY"))
    if not usa_or and not usa_gem:
        scrivi(os.path.join(cartella, f"ERRORE_{cod}.txt"),
               f"# postino: {cod} non interrogato il {ora_roma():%d/%m/%Y %H:%M}: manca la chiave OPENROUTER_API_KEY "
               f"o un modello adatto ({modelli.get(cod) or 'nessun modello'}).\n")
        print(f"{cod}: saltato (nessuna chiave o modello)")
        return

    def una_chiamata(storia=None):
        if usa_or:
            try:
                ric = "no" if conf["limiti"].get("solo_gratis") else c.get("ricerca", "web")
                return chiama_openrouter(modelli[cod], sistema, testo_pacchetto, ric, conf, storia)
            except Exception as e:  # noqa: BLE001
                if not usa_gem:
                    raise
                print("  OpenRouter fallito, passo a Gemini diretto:", e)
        return chiama_gemini_diretto(sistema, testo_pacchetto, storia)

    tentativi, costo, esito, mancanze = 0, 0.0, None, []
    try:
        storia = None
        for _ in range(2):
            tentativi += 1
            esito = una_chiamata(storia)
            costo += esito["costo"] or 0.0
            esito["testo"] = pulisci(esito["testo"])
            mancanze = controlla(esito["testo"], cod, date_ok)
            if not mancanze:
                break
            print(f"  formato incompleto ({', '.join(mancanze)}): chiedo una correzione")
            storia = [{"role": "assistant", "content": esito["testo"]},
                      {"role": "user", "content": "La risposta non rispetta il FORMATO DI RISPOSTA della sezione 14: manca "
                       + "; ".join(mancanze) + ". Riscrivi tutta la risposta completa, solo il blocco del formato, testo semplice."}]
    except Exception as e:  # noqa: BLE001
        scrivi(os.path.join(cartella, f"ERRORE_{cod}.txt"),
               f"# postino: nessuna risposta da {cod} ({modelli.get(cod, 'Gemini diretto')}) il {ora_roma():%d/%m/%Y %H:%M}\n# errore: {e}\n")
        print(f"{cod}: ERRORE {e}")
        sys.exit(0)

    adesso = ora_roma()
    intest = [
        "# RISPOSTA AUTOMATICA – Sfida Portafogli AI (righe con # scritte dal postino, non dall'AI)",
        f"# concorrente: {cod} ({c['nome']})",
        f"# al bollettino del: {data} (giorno {os.environ.get('GIORNO', '')})",
        f"# ricevuta: {adesso:%Y-%m-%d %H:%M} ora italiana ({adesso.astimezone(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ})",
        f"# modello: {esito['modello']} · ricerca web: {esito['ricerca']} · tentativi: {tentativi} · secondi: {esito['secondi']}"
        + (f" · costo: {costo:.4f} $" if costo else ""),
        "# formato: " + ("valido" if not mancanze else "INCOMPLETO – manca " + "; ".join(mancanze)),
    ]
    scrivi(os.path.join(cartella, f"risposta_{cod}.txt"), "\n".join(intest) + "\n\n" + esito["testo"].strip() + "\n")
    errore_vecchio = os.path.join(cartella, f"ERRORE_{cod}.txt")
    if os.path.exists(errore_vecchio):
        os.remove(errore_vecchio)
    print(f"{cod}: risposta salvata ({esito['modello']}, {'formato valido' if not mancanze else 'formato incompleto'})")


def _intestazione(p):
    h = {}
    for r in leggi(p).splitlines():
        if not r.startswith("# "):
            break
        if ":" in r:
            k, v = r[2:].split(":", 1)
            h[k.strip()] = v.strip()
    return h


def _sintesi_operazioni(p):
    t = leggi(p)
    if "NESSUNA OPERAZIONE" in t.split("BEST 2", 1)[0]:
        return "nessuna operazione"
    ops = re.findall(r"^\s*-\s*(COMPRA|RAFFORZA|ALLEGGERISCI|VENDI|LIVELLI|CONDIZIONATO|ANNULLA)\s*\|\s*([^|\n]+)", t, re.M)
    return ", ".join(f"{a} {b.strip()}" for a, b in ops[:5]) or "–"


def cmd_raccogli():
    conf = cfg()
    cartella = os.path.join(RADICE, os.environ["CARTELLA"])
    data, modalita = os.environ.get("DATA", ""), os.environ.get("MODALITA", "gara")
    righe = [f"# Ultimo giro del postino ({'PROVA' if modalita == 'prova' else 'gara'})", "",
             f"Bollettino del **{data}** · controllato il {ora_roma():%d/%m/%Y alle %H:%M} (ora italiana) · cartella `{os.environ['CARTELLA']}`", "",
             "| AI | esito | ricevuta | modello | operazioni | costo $ |", "|---|---|---|---|---|---|"]
    tot, ok = 0.0, 0
    for c in conf["concorrenti"]:
        cod = c["cod"]
        r, e = os.path.join(cartella, f"risposta_{cod}.txt"), os.path.join(cartella, f"ERRORE_{cod}.txt")
        if os.path.exists(r):
            h = _intestazione(r)
            mod = h.get("modello", "").split(" · ")[0]
            m = re.search(r"costo: ([\d.]+)", h.get("modello", ""))
            costo = float(m.group(1)) if m else 0.0
            tot += costo
            stato = "✅" if h.get("formato", "").startswith("valido") else "⚠️ formato incompleto"
            ok += 1
            righe.append(f"| {cod} | {stato} | {h.get('ricevuta', '')[:16]} | {mod} | {_sintesi_operazioni(r)} | {costo:.3f} |")
        elif os.path.exists(e):
            motivo = " ".join(x[2:] for x in leggi(e).splitlines() if x.startswith("# "))[:160]
            righe.append(f"| {cod} | ❌ | – | – | {motivo} | – |")
        elif c.get("attivo", True):
            righe.append(f"| {cod} | – non interrogato | – | – | – | – |")
    righe += ["", f"Risposte arrivate: **{ok}** su {len(conf['concorrenti'])} · costo della serata: **{tot:.2f} $**"]
    testo = "\n".join(righe) + "\n"
    scrivi(os.path.join(RADICE, "stato", "ultimo_giro.md" if modalita == "gara" else "ultima_prova.md"), testo)
    riepilogo(testo)


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else ""
    if comando == "prepara":
        cmd_prepara()
    elif comando == "chiedi":
        cmd_chiedi(sys.argv[2].upper())
    elif comando == "raccogli":
        cmd_raccogli()
    else:
        print(__doc__)
        sys.exit(1)
