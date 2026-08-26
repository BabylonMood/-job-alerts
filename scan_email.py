#!/usr/bin/env python3
"""
scan_email.py
-------------
Lee una casilla IMAP donde llegan las alertas de LinkedIn / Computrabajo /
Bumeran / ZonaJobs / Indeed (configuradas por vos en cada sitio), extrae las
ofertas de esos mails, las filtra por palabras clave y actualiza jobs.json
para que el dashboard (index.html) las muestre.

IMPORTANTE:
- Esto NO scrapea las páginas de empleo. Lee correos que los propios portales
  te mandan porque vos configuraste la alerta ahí. Es la forma legal y estable
  de hacer esto: si scrapeás LinkedIn directo te banean la cuenta o la IP.
- Los extractores son heurísticos (best effort). El de LinkedIn intenta sacar
  varias ofertas de cada mail de resumen ("digest") y limpia las URLs de
  parámetros de tracking. Si un portal cambia el formato de sus mails, hay que
  retocar la función extract_<portal> correspondiente.
"""

import imaplib
import email
from email.header import decode_header
import json
import re
import os
import sys
import hashlib
import unicodedata
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

CONFIG_PATH = os.environ.get("JOB_ALERT_CONFIG", "config.json")
JOBS_PATH = os.environ.get("JOB_ALERT_JOBS", "jobs.json")
SEEN_PATH = os.environ.get("JOB_ALERT_SEEN", "seen.json")
MAX_JOBS_STORED = 300
MAX_SEEN_STORED = 2000
MAX_JOB_AGE_DAYS = 30


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"No encontré {CONFIG_PATH}. Copiá config.example.json a config.json y completalo.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_text(s):
    s = unicodedata.normalize("NFC", s or "")
    return s.replace("\u200b", "").replace("\ufeff", "").replace("\r", " ")


def normalize_for_match(s):
    s = normalize_text(s).lower()
    s = "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")
    return s


def decode_mime(s):
    if s is None:
        return ""
    parts = decode_header(s)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="ignore")
        else:
            out += text
    return out


def get_body(msg):
    """Devuelve el cuerpo en texto plano (o HTML como fallback) del mail."""
    if msg.is_multipart():
        text_plain, text_html = "", ""
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="ignore")
            except Exception:
                continue
            if ctype == "text/plain":
                text_plain += decoded
            elif ctype == "text/html":
                text_html += decoded
        return text_plain or text_html
    try:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="ignore") if payload else ""
    except Exception:
        return ""


def strip_html(html):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</div>|</tr>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def find_urls(text):
    return re.findall(r"https?://[^\s\"'<>]+", text)


# ---------------------------------------------------------------------------
# Limpieza de URLs y frases de relleno típicas de los mails
# ---------------------------------------------------------------------------

TRACKING_PARAMS = {
    "refid", "trackingid", "lipi", "midtoken", "midsig", "trk", "trkemail",
    "eid", "otptoken", "ref", "gclid", "fbclid",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
}

BOILERPLATE_FRAGMENTS = [
    "tu alerta de empleo para",
    "tus alertas de empleo",
    "nuevos empleos coinciden",
    "empleos coinciden con tus preferencias",
    "resultados de la nueva búsqueda de empleo",
    "búsqueda de empleo con ia",
    "esta empresa busca personal activamente",
    "solicitar con perfil y cv",
    "ver anuncio de empleo",
    "se muestran a continuación",
    "no deseo recibir",
    "algunas de estas",
    "para que no vuelvan a aparecer",
    "haz clic aquí",
    "empleos recomendados",
    "no responda este correo",
    "el empleo guardado de",
    "sigue disponible",
    "tu empleo caduca",
    "conserva tu candidatura",
    "los datos de contacto de",
]


def clean_url(url, source="otro"):
    url = (url or "").strip()
    if not url:
        return ""
    url = url.split("#")[0]
    if source == "linkedin":
        return url.split("?")[0]
    base, _, query = url.partition("?")
    if not query:
        return url
    kept = [pair for pair in query.split("&") if pair.split("=", 1)[0].lower() not in TRACKING_PARAMS]
    return base + ("?" + "&".join(kept) if kept else "")


# ---------------------------------------------------------------------------
# Extractores por portal. Cada uno devuelve una LISTA de ofertas (dict).
# ---------------------------------------------------------------------------

def clean_line(line):
    line = line.strip()
    line = re.sub(r"^\s*[•·\-–—|*]\s*", "", line)
    line = re.sub(r"\s+", " ", line)
    return line


def is_boilerplate(line):
    low = normalize_text(line).lower()
    return any(frag in low for frag in BOILERPLATE_FRAGMENTS)


def extract_linkedin(subject, text, urls):
    """Saca varias ofertas de los mails de resumen de LinkedIn."""
    text = normalize_text(text)
    parts = re.split(r"(?i)ver\s+anuncio\s+de\s+empleo\s*:?\s*(https?://[^\s]+)", text)
    jobs = []
    for i in range(1, len(parts), 2):
        url = clean_url(parts[i], "linkedin")
        body = parts[i - 1]
        lines = []
        for raw in body.splitlines():
            line = clean_line(raw)
            if not line or is_boilerplate(line):
                continue
            lines.append(line)
        if not lines:
            continue
        title = lines[0]
        company = lines[1] if len(lines) > 1 else ""
        company = re.sub(r"\s*[-–]\s*.*$", "", company).strip()
        location = lines[2] if len(lines) > 2 else ""
        snippet = " · ".join(l for l in lines[:3] if l)[:300]
        jobs.append({
            "title": title,
            "company": company or "No especificada",
            "url": url,
            "snippet": snippet,
        })
    if not jobs:
        single = extract_generic(subject, text, urls)
        if single:
            jobs.append(single)
    return jobs


def extract_generic(subject, text, urls):
    """Fallback: usa el asunto como título y la primera URL como link."""
    subject = normalize_text(subject)
    subject = re.sub(r"^(re|fwd|rv|enc?|aw)\s*:\s*", "", subject, flags=re.I).strip()
    title, company = subject, ""

    m = re.match(r"^«(.+?)»\s*:\s*(.+)$", subject)
    if m:
        title, company = m.group(1).strip(), m.group(2).strip()
    else:
        cleaned = subject.strip("«»\"' \u201c\u201d")
        m = re.search(r"(.+?)\s+(?:en|at|-)\s+(.+)", cleaned)
        if m:
            title, company = m.group(1).strip(), m.group(2).strip()

    if not title:
        return None
    url = clean_url(urls[0], "otro") if urls else ""
    snippet = re.sub(r"\s+", " ", text).strip()[:300]
    return {
        "title": title,
        "company": company or "No especificada",
        "url": url,
        "snippet": snippet,
    }


EXTRACTORS = {
    "linkedin": extract_linkedin,
    "computrabajo": extract_generic,
    "bumeran": extract_generic,
    "zonajobs": extract_generic,
    "indeed": extract_generic,
    "otro": extract_generic,
}


def detect_source(from_addr):
    from_addr = from_addr.lower()
    if "linkedin" in from_addr:
        return "linkedin"
    if "computrabajo" in from_addr:
        return "computrabajo"
    if "bumeran" in from_addr:
        return "bumeran"
    if "zonajobs" in from_addr:
        return "zonajobs"
    if "indeed" in from_addr:
        return "indeed"
    return "otro"


def keyword_match(title, snippet, config):
    text = normalize_for_match((title or "") + " " + (snippet or ""))
    includes = config.get("keywords_include", []) or []
    excludes = config.get("keywords_exclude", []) or []
    if excludes and any(normalize_for_match(k) in text for k in excludes):
        return None
    if not includes:
        return []
    matched = [k for k in includes if normalize_for_match(k) in text]
    return matched if matched else None


def _parse_date(iso_str):
    try:
        return datetime.fromisoformat(iso_str or "2000-01-01T00:00:00+00:00")
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def make_id(source, title, company, url):
    raw = f"{source}|{title}|{company}|{url}".lower()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "1",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                print(f"  Telegram: respuesta inesperada {resp.status}")
    except Exception as e:
        print(f"  Telegram: no pude enviar notificación: {e}")


def build_telegram_message(new_jobs):
    lines = [f"*🆕 {len(new_jobs)} oferta(s) nueva(s)*\n"]
    for j in new_jobs[:10]:
        source = j.get("source", "otro")
        title = j.get("title", "Sin título")
        company = j.get("company", "")
        url = j.get("url", "")
        kws = ", ".join(j.get("matched_keywords", []))
        block = f"*{esc_md(title)}*"
        if company:
            block += f"\n{esc_md(company)}"
        if kws:
            block += f"\n🔑 {esc_md(kws)}"
        if url:
            block += f"\n[Ver oferta]({url})"
        lines.append(block)
    if len(new_jobs) > 10:
        lines.append(f"\n_...y {len(new_jobs) - 10} más_")
    return "\n\n".join(lines)


def esc_md(s):
    text = str(s or "")
    for ch in ("_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"):
        text = text.replace(ch, "\\" + ch)
    return text


def main():
    config = load_config()
    seen = load_json(SEEN_PATH, [])
    seen_set = set(seen)
    jobs = load_json(JOBS_PATH, [])
    existing_status = {j.get("id"): j.get("status", "new") for j in jobs if j.get("id")}

    imap_host = config["imap_host"]
    imap_user = os.environ.get("IMAP_USER", config.get("imap_user", ""))
    imap_pass = os.environ.get("IMAP_PASS", config.get("imap_pass", ""))
    senders = config.get("senders", [])

    if not imap_user or not imap_pass:
        sys.exit("Falta IMAP_USER / IMAP_PASS (como variable de entorno o en config.json).")

    print(f"Conectando a {imap_host} como {imap_user}...")
    M = imaplib.IMAP4_SSL(imap_host)
    try:
        M.login(imap_user, imap_pass)
        M.select("INBOX")

        new_jobs = []
        for sender in senders:
            typ, data = M.search(None, f'(FROM "{sender}" UNSEEN)')
            if typ != "OK":
                continue
            ids = data[0].split()
            print(f"  {sender}: {len(ids)} mail(es) nuevo(s)")
            for mail_id in ids:
                try:
                    typ, msg_data = M.fetch(mail_id, "(RFC822)")
                    if typ != "OK":
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])
                    subject = decode_mime(msg.get("Subject"))
                    from_addr = decode_mime(msg.get("From"))
                    raw_body = get_body(msg)
                    is_html = "<html" in raw_body.lower() or "<body" in raw_body.lower()
                    text = strip_html(raw_body) if is_html else raw_body
                    urls = find_urls(raw_body)

                    source = detect_source(from_addr)
                    extractor = EXTRACTORS.get(source, extract_generic)
                    result = extractor(subject, text, urls)
                    candidates = result if isinstance(result, list) else [result]

                    for extracted in candidates:
                        if not extracted:
                            continue
                        matched = keyword_match(extracted["title"], extracted["snippet"], config)
                        if matched is None:
                            continue

                        job_id = make_id(source, extracted["title"], extracted["company"], extracted["url"])
                        if job_id in seen_set:
                            continue

                        job = {
                            "id": job_id,
                            "title": extracted["title"],
                            "company": extracted["company"],
                            "source": source,
                            "url": extracted["url"],
                            "matched_keywords": matched,
                            "found_at": datetime.now(timezone.utc).isoformat(),
                            "snippet": extracted["snippet"],
                            "status": existing_status.get(job_id, "new"),
                        }
                        new_jobs.append(job)
                        seen_set.add(job_id)
                except Exception as e:
                    print(f"    ⚠ No pude procesar un mail: {e}")

        if new_jobs:
            new_ids = {j["id"] for j in new_jobs}
            jobs = new_jobs + [j for j in jobs if j.get("id") not in new_ids]
            print(f"✅ {len(new_jobs)} oferta(s) nueva(s) agregada(s)")
            send_telegram(build_telegram_message(new_jobs))

        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_JOB_AGE_DAYS)
        before = len(jobs)
        jobs = [j for j in jobs if _parse_date(j.get("found_at")) > cutoff]
        pruned = before - len(jobs)
        if pruned:
            print(f"🗑 {pruned} oferta(s) vencida(s) eliminada(s) (+{MAX_JOB_AGE_DAYS} días)")

        jobs = jobs[:MAX_JOBS_STORED]
        save_json(JOBS_PATH, jobs)
        save_json(SEEN_PATH, list(seen_set)[-MAX_SEEN_STORED:])
        if not new_jobs and not pruned:
            print("Sin ofertas nuevas que matcheen tus keywords.")
    finally:
        try:
            M.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
