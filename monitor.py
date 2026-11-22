#!/usr/bin/env python3
"""Monitor gratuito de preços do PlayStation 5 (Edição Digital e Com Leitor).

Usa apenas a biblioteca padrão do Python (zero dependências externas).
Consulta comparadores e lojas brasileiras (Zoom, Buscapé, KaBuM! e URLs extras),
filtra apenas consoles PS5 válidos (descartando acessórios, jogos e PS5 Pro),
evita alertas repetidos usando state.json e envia notificações via Gmail SMTP.
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import smtplib
import ssl
import sys
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, urljoin, urlsplit, urlunsplit

BRT = timezone(timedelta(hours=-3))

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip",
}

# Prefixos que indicam que o item em si é um acessório/jogo, e não o console.
ACCESSORY_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"controle|jogo|game|tampa|tampas|capa|leitor\s+de\s+disco|unidade\s+de\s+disco|"
    r"suporte|base\s+carregadora|carregador|cooler|ventoinha|cabo|fone|headset|"
    r"ssd|bolsa|mochila|case|skin|adesivo|pelicula|volante|cadeira|mesa|rack|"
    r"fonte|placa|peca|carcaca|gift\s*card|cartao\s+presente|assinatura"
    r")\b"
)

# Termos em qualquer parte do título que excluem o anúncio.
EXCLUDED_PHRASES = (
    "ps5 pro",
    "playstation 5 pro",
    "playstation portal",
    "ps portal",
    "ps vr2",
    "psvr2",
    "psvr 2",
    "para ps5",
    "para playstation 5",
    "para console playstation 5",
    "para o console playstation 5",
    "leitor de disco para",
    "unidade de disco para",
    "tampa para",
    "tampas do console",
    "capa para",
    "suporte para",
    "cooler para",
    "usado",
    "seminovo",
    "semi novo",
    "recondicionado",
    "vitrine",
    "com defeito",
    "retirada de pecas",
)

PS5_MENTION_RE = re.compile(r"\b(?:ps5|playstation\s*5)\b")


@dataclass
class Offer:
    source: str
    title: str
    price: float
    url: str
    edition: str = "disc"  # "disc" ou "digital"
    seller: str = ""

    @property
    def canonical_url(self) -> str:
        return strip_tracking_params(self.url)

    @property
    def edition_label(self) -> str:
        return "Edição Digital" if self.edition == "digital" else "Com Leitor de Disco"


def normalize_text(text: str) -> str:
    """Remove acentos, converte para minúsculas e normaliza espaços."""
    text = html.unescape(text or "")
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", ascii_text).strip().lower()


def strip_tracking_params(url: str) -> str:
    """Remove parâmetros de rastreamento da URL para deduplicação estável."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def parse_brl_price(raw: Any) -> float | None:
    """Converte strings como 'R$ 3.299,90' ou números para float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if val > 0 else None

    text = str(raw).strip()
    m = re.search(r"(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2}|\d+\.\d{2})", text)
    if not m:
        return None
    num = m.group(1)
    if "," in num:
        num = num.replace(".", "").replace(",", ".")
    try:
        val = float(num)
        return val if val > 0 else None
    except ValueError:
        return None


def format_brl(value: float) -> str:
    """Formata float como 'R$ 3.299,90'."""
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def classify_edition(title: str) -> str:
    """Classifica o console entre 'digital' e 'disc' (com leitor de disco)."""
    norm = normalize_text(title)
    if "digital" in norm and "com leitor" not in norm:
        return "digital"
    return "disc"


def is_valid_ps5_console(
    title: str,
    price: float | None,
    min_price: float,
    allowed_editions: set[str],
) -> tuple[bool, str]:
    """Valida se o item é um console PS5 válido dentro dos filtros configurados."""
    if price is None or price < min_price:
        return False, "abaixo do preço mínimo (provável acessório/jogo)"

    norm = normalize_text(title)
    if not PS5_MENTION_RE.search(norm):
        return False, "não menciona PS5/PlayStation 5"

    if ACCESSORY_PREFIX_RE.search(norm):
        return False, "prefixo de acessório/jogo"

    for phrase in EXCLUDED_PHRASES:
        if phrase in norm:
            return False, f"contém termo excluído: {phrase}"

    edition = classify_edition(title)
    if edition not in allowed_editions:
        return False, f"edição não selecionada: {edition}"

    return True, edition


def fetch_html(url: str, timeout: int = 20) -> str | None:
    """Faz requisição HTTP GET seguindo redirecionamentos (inclusive 308)."""
    ctx = ssl.create_default_context()
    current_url = url
    for _ in range(5):
        req = urllib.request.Request(current_url, headers=DEFAULT_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                current_url = urljoin(current_url, exc.headers["Location"])
                continue
            print(f"[AVISO] HTTP {exc.code} ao acessar {current_url}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[AVISO] Falha de rede ao acessar {current_url}: {exc}")
            return None
    return None


class _MosaicoCardParser(HTMLParser):
    """Parser HTML nativo para cards de produto do Zoom e Buscapé."""

    def __init__(self) -> None:
        super().__init__()
        self.cards: list[dict[str, str]] = []
        self._in_card = False
        self._card_depth = 0
        self._current: dict[str, str] = {}
        self._capture_field: str | None = None
        self._capture_depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        testid = attr_map.get("data-testid") or ""

        if not self._in_card and tag == "a" and testid == "product-card::card":
            self._in_card = True
            self._card_depth = 1
            self._current = {"href": attr_map.get("href") or "", "name": "", "price": ""}
            return

        if self._in_card:
            self._card_depth += 1
            if self._capture_field is not None:
                self._capture_depth += 1
            elif testid == "product-card::name":
                self._capture_field = "name"
                self._capture_depth = 1
                self._buf = []
            elif testid == "product-card::price":
                self._capture_field = "price"
                self._capture_depth = 1
                self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture_field is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_card:
            return

        if self._capture_field is not None:
            self._capture_depth -= 1
            if self._capture_depth <= 0:
                text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
                self._current[self._capture_field] = text
                self._capture_field = None
                self._buf = []

        self._card_depth -= 1
        if self._card_depth <= 0:
            self.cards.append(self._current)
            self._in_card = False
            self._current = {}


class _PageMetadataParser(HTMLParser):
    """Extrai scripts JSON (__NEXT_DATA__, ld+json), meta tags e <title>."""

    def __init__(self) -> None:
        super().__init__()
        self.next_data_raw: str | None = None
        self.ld_json_blocks: list[str] = []
        self.metas: dict[str, str] = {}
        self.title: str = ""
        self._in_script: str | None = None
        self._in_title = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        t = tag.lower()
        if t == "script":
            if attr_map.get("id") == "__NEXT_DATA__":
                self._in_script = "next"
                self._buf = []
            elif "ld+json" in attr_map.get("type", "").lower():
                self._in_script = "ld"
                self._buf = []
        elif t == "title":
            self._in_title = True
            self._buf = []
        elif t == "meta":
            content = attr_map.get("content", "").strip()
            if content:
                for key_attr in ("property", "itemprop", "name"):
                    key = attr_map.get(key_attr, "").strip().lower()
                    if key and key not in self.metas:
                        self.metas[key] = content

    def handle_data(self, data: str) -> None:
        if self._in_script or self._in_title:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "script" and self._in_script:
            raw = "".join(self._buf).strip()
            if self._in_script == "next":
                self.next_data_raw = raw
            elif self._in_script == "ld" and raw:
                self.ld_json_blocks.append(raw)
            self._in_script = None
            self._buf = []
        elif t == "title" and self._in_title:
            self.title = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self._in_title = False
            self._buf = []


def parse_mosaico_cards(html_text: str, source_name: str, base_url: str) -> list[Offer]:
    """Extrai ofertas de resultados do Zoom ou Buscapé."""
    parser = _MosaicoCardParser()
    parser.feed(html_text)
    offers: list[Offer] = []

    for card in parser.cards:
        href = card.get("href", "").strip()
        title = card.get("name", "").strip()
        price = parse_brl_price(card.get("price"))
        if not href or not title or price is None:
            continue

        full_url = urljoin(base_url, href)
        offers.append(
            Offer(
                source=source_name,
                title=title,
                price=price,
                url=strip_tracking_params(full_url),
                edition=classify_edition(title),
                seller=f"Comparador {source_name}",
            )
        )
    return offers


def fetch_mosaico_search(
    source_name: str,
    base_url: str,
    queries: list[str],
) -> list[Offer]:
    all_offers: list[Offer] = []
    for query in queries:
        url = f"{base_url}/search?q={quote_plus(query)}"
        body = fetch_html(url)
        if not body:
            continue
        found = parse_mosaico_cards(body, source_name, base_url)
        print(f"[{source_name}] Consulta '{query}': {len(found)} cards lidos")
        all_offers.extend(found)
    return all_offers


def parse_kabum_page(html_text: str) -> list[Offer]:
    """Extrai ofertas do KaBuM! via __NEXT_DATA__ (com fallback para JSON-LD)."""
    offers: list[Offer] = []
    meta = _PageMetadataParser()
    meta.feed(html_text)

    if meta.next_data_raw:
        try:
            data = json.loads(meta.next_data_raw)
            page_data = data.get("props", {}).get("pageProps", {}).get("data")
            if isinstance(page_data, str):
                page_data = json.loads(page_data)
            if not isinstance(page_data, dict):
                page_data = {}
            catalog = page_data.get("catalogServer")
            items = catalog.get("data", []) if isinstance(catalog, dict) else []
            for item in items:
                if not isinstance(item, dict) or item.get("available") is False:
                    continue
                name = str(item.get("name") or "").strip()
                price = parse_brl_price(item.get("priceWithDiscount")) or parse_brl_price(
                    item.get("price")
                )
                code = item.get("code")
                slug = item.get("friendlyName") or ""
                if not name or price is None or not code:
                    continue
                url = (
                    f"https://www.kabum.com.br/produto/{code}/{slug}"
                    if slug
                    else f"https://www.kabum.com.br/produto/{code}"
                )
                seller = str(item.get("sellerName") or "KaBuM!").strip()
                offers.append(
                    Offer(
                        source="KaBuM!",
                        title=name,
                        price=price,
                        url=url,
                        edition=classify_edition(name),
                        seller=seller,
                    )
                )
            if offers:
                return offers
        except (ValueError, TypeError, AttributeError) as exc:
            print(f"[AVISO] Falha ao ler __NEXT_DATA__ do KaBuM!: {exc}")

    # Fallback via JSON-LD Product
    for raw in meta.ld_json_blocks:
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("@type") != "Product":
                continue
            name = str(entry.get("name") or "").strip()
            offer_data = entry.get("offers") or {}
            if isinstance(offer_data, list):
                offer_data = offer_data[0] if offer_data else {}
            if not isinstance(offer_data, dict):
                continue
            price = parse_brl_price(offer_data.get("price") or offer_data.get("lowPrice"))
            url = str(offer_data.get("url") or entry.get("url") or "").strip()
            if name and price and url:
                offers.append(
                    Offer(
                        source="KaBuM!",
                        title=name,
                        price=price,
                        url=strip_tracking_params(url),
                        edition=classify_edition(name),
                        seller="KaBuM!",
                    )
                )
    return offers


def fetch_kabum_search(queries: list[str]) -> list[Offer]:
    all_offers: list[Offer] = []
    for query in queries:
        slug = quote(query.strip().replace(" ", "-"))
        url = f"https://www.kabum.com.br/busca/{slug}"
        body = fetch_html(url)
        if not body:
            continue
        found = parse_kabum_page(body)
        print(f"[KaBuM!] Consulta '{query}': {len(found)} produtos lidos")
        all_offers.extend(found)
    return all_offers


def parse_generic_product_page(html_text: str, url: str, label: str = "") -> Offer | None:
    """Extrator genérico via JSON-LD (schema.org/Product) ou meta tags de preço."""
    meta = _PageMetadataParser()
    meta.feed(html_text)
    host = urlsplit(url).netloc.replace("www.", "")
    source = label or host or "URL Customizada"

    for raw in meta.ld_json_blocks:
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        expanded: list[dict[str, Any]] = []
        for item in candidates:
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    expanded.extend(x for x in item["@graph"] if isinstance(x, dict))
                else:
                    expanded.append(item)
        for entry in expanded:
            if entry.get("@type") != "Product":
                continue
            name = str(entry.get("name") or "").strip()
            offers_obj = entry.get("offers")
            if isinstance(offers_obj, list):
                offers_obj = offers_obj[0] if offers_obj else {}
            if not isinstance(offers_obj, dict):
                continue
            availability = str(offers_obj.get("availability") or "").lower()
            if "outofstock" in availability:
                return None
            price = parse_brl_price(offers_obj.get("price") or offers_obj.get("lowPrice"))
            if name and price:
                return Offer(
                    source=source,
                    title=name,
                    price=price,
                    url=strip_tracking_params(url),
                    edition=classify_edition(name),
                    seller=source,
                )

    raw_price = (
        meta.metas.get("product:price:amount")
        or meta.metas.get("og:price:amount")
        or meta.metas.get("price")
    )
    if raw_price:
        price = parse_brl_price(raw_price)
        title = meta.metas.get("og:title") or meta.title or source
        if price:
            return Offer(
                source=source,
                title=title,
                price=price,
                url=strip_tracking_params(url),
                edition=classify_edition(title),
                seller=source,
            )
    return None


def fetch_custom_urls(entries: list[Any]) -> list[Offer]:
    offers: list[Offer] = []
    for entry in entries:
        if isinstance(entry, str):
            url, label = entry, ""
        elif isinstance(entry, dict) and entry.get("url"):
            url = str(entry["url"])
            label = str(entry.get("name") or "")
        else:
            continue
        body = fetch_html(url)
        if not body:
            continue
        offer = parse_generic_product_page(body, url, label=label)
        if offer:
            print(f"[URL Extra] {offer.title} -> {format_brl(offer.price)}")
            offers.append(offer)
        else:
            print(f"[AVISO] Não foi possível extrair preço de {url}")
    return offers


def deduplicate_offers(offers: list[Offer]) -> list[Offer]:
    """Remove ofertas duplicadas (mesma URL ou mesmo produto/preço no Zoom+Buscapé)."""
    by_url: dict[str, Offer] = {}
    for offer in offers:
        key = offer.canonical_url
        if key not in by_url or offer.price < by_url[key].price:
            by_url[key] = offer

    seen_title_price: set[tuple[str, float]] = set()
    result: list[Offer] = []
    for offer in sorted(by_url.values(), key=lambda o: (o.price, o.source != "Zoom")):
        sig = (normalize_text(offer.title), round(offer.price, 2))
        if sig in seen_title_price and offer.source in {"Zoom", "Buscapé"}:
            continue
        seen_title_price.add(sig)
        result.append(offer)
    return result


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def classify_alert_tier(price: float, alert_price: float, super_alert_price: float) -> str | None:
    """Classifica o nível do alerta: 'super', 'alert' ou None."""
    if price <= super_alert_price:
        return "super"
    if price <= alert_price:
        return "alert"
    return None


def select_offers_to_notify(
    deals: list[Offer],
    state: dict[str, dict[str, Any]],
    alert_price: float,
    super_alert_price: float,
    min_price_drop: float,
    renotify_after_hours: float,
    now: datetime,
) -> list[Offer]:
    """Decide quais ofertas devem disparar e-mail sem gerar spam repetido."""
    to_notify: list[Offer] = []
    for deal in deals:
        tier = classify_alert_tier(deal.price, alert_price, super_alert_price)
        if not tier:
            continue

        prev = state.get(deal.canonical_url)
        if not prev:
            to_notify.append(deal)
            continue

        prev_tier = prev.get("tier", "alert")
        prev_price = float(prev.get("price", 0))
        prev_time_str = prev.get("notified_at")
        hours_since = float("inf")
        if prev_time_str:
            try:
                prev_dt = datetime.fromisoformat(prev_time_str)
                hours_since = (now - prev_dt).total_seconds() / 3600.0
            except ValueError:
                pass

        upgraded_to_super = tier == "super" and prev_tier != "super"
        dropped_enough = deal.price <= (prev_price - min_price_drop)
        expired_cooldown = hours_since >= renotify_after_hours
        if upgraded_to_super or dropped_enough or expired_cooldown:
            to_notify.append(deal)

    return to_notify


def build_email_content(
    deals: list[Offer],
    alert_price: float,
    super_alert_price: float,
    now: datetime,
    is_test: bool = False,
) -> tuple[str, str, str]:
    """Monta assunto, versão texto puro e versão HTML do e-mail com níveis de alerta."""
    timestamp = now.astimezone(BRT).strftime("%d/%m/%Y às %H:%M (BRT)")

    if is_test:
        subject = "✅ Teste do Monitor de Preço do PS5 — Funcionando!"
        plain = (
            f"Seu monitor gratuito de PS5 está configurado corretamente!\n"
            f"Data do teste: {timestamp}\n"
            f"🚨 Super Alerta: até {format_brl(super_alert_price)}\n"
            f"🔥 Alerta de Promoção: até {format_brl(alert_price)}\n\n"
            f"Assim que um PS5 (Com Leitor ou Digital) entrar nessas faixas, "
            f"você receberá um e-mail automático com o link da oferta."
        )
        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #1f2937;">
          <h2 style="color: #059669; margin-top: 0;">✅ Monitor de PS5 funcionando!</h2>
          <p>A integração com seu Gmail via GitHub Actions está ativa e rodando automaticamente.</p>
          <p>
            <strong>🚨 Super Alerta:</strong> até {format_brl(super_alert_price)}<br>
            <strong>🔥 Alerta de Promoção:</strong> até {format_brl(alert_price)}<br>
            <strong>Modelos monitorados:</strong> PS5 Com Leitor de Disco e Edição Digital<br>
            <strong>Horário do teste:</strong> {timestamp}
          </p>
          <p style="color: #6b7280; font-size: 13px;">Você só receberá novos e-mails quando surgir uma oferta real dentro dessas faixas.</p>
        </div>
        """
        return subject, plain, html_body

    best = deals[0]
    has_super = any(d.price <= super_alert_price for d in deals)

    if has_super:
        subject = f"🚨 SUPER ALERTA! PS5 por {format_brl(best.price)} ({best.edition_label})"
        header_title = f"🚨 SUPER ALERTA! PS5 por {format_brl(best.price)}!"
        header_color = "#dc2626"
    else:
        subject = f"🔥 Alerta PS5: {format_brl(best.price)} ({best.edition_label})"
        header_title = f"🔥 PS5 em Promoção (até {format_brl(alert_price)})"
        header_color = "#d97706"

    plain_lines = [
        f"{header_title}",
        f"Encontradas {len(deals)} oferta(s) — Verificado em {timestamp}",
        f"Faixas: 🚨 Super Alerta <= {format_brl(super_alert_price)} | 🔥 Alerta <= {format_brl(alert_price)}",
        "",
    ]
    cards_html = []
    for deal in deals:
        is_super = deal.price <= super_alert_price
        tier_label = "🚨 SUPER ALERTA" if is_super else "🔥 ALERTA"
        tier_bg = "#fee2e2" if is_super else "#fef3c7"
        tier_fg = "#b91c1c" if is_super else "#b45309"
        border_color = "#ef4444" if is_super else "#e5e7eb"
        price_color = "#dc2626" if is_super else "#059669"

        plain_lines.append(
            f"[{tier_label}] {deal.title}\n"
            f"  Preço: {format_brl(deal.price)} | {deal.edition_label} | Fonte: {deal.source}\n"
            f"  Link: {deal.url}\n"
        )
        badge_bg = "#dbeafe" if deal.edition == "digital" else "#ede9fe"
        badge_fg = "#1d4ed8" if deal.edition == "digital" else "#6d28d9"
        seller_suffix = (
            f" · {html.escape(deal.seller)}"
            if deal.seller and deal.seller != deal.source
            else ""
        )
        cards_html.append(
            f"""
            <div style="border: 2px solid {border_color}; border-radius: 10px; padding: 16px; margin-bottom: 14px; background: #ffffff;">
              <div style="margin-bottom: 8px;">
                <span style="display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; background: {tier_bg}; color: {tier_fg}; margin-right: 6px;">
                  {tier_label}
                </span>
                <span style="display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; background: {badge_bg}; color: {badge_fg};">
                  {html.escape(deal.edition_label)}
                </span>
                <span style="font-size: 12px; color: #6b7280; margin-left: 8px;">
                  {html.escape(deal.source)}{seller_suffix}
                </span>
              </div>
              <div style="font-size: 16px; font-weight: 600; color: #111827; margin-bottom: 8px;">
                {html.escape(deal.title)}
              </div>
              <div style="font-size: 26px; font-weight: 700; color: {price_color}; margin-bottom: 12px;">
                {format_brl(deal.price)}
              </div>
              <a href="{html.escape(deal.url)}" style="display: inline-block; background: #2563eb; color: #ffffff; text-decoration: none; padding: 10px 16px; border-radius: 8px; font-weight: 600; font-size: 14px;">
                Ver oferta →
              </a>
            </div>
            """
        )

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 640px; margin: 0 auto; padding: 20px; background: #f9fafb; color: #1f2937;">
      <h2 style="margin-top: 0; color: {header_color};">{html.escape(header_title)}</h2>
      <p style="color: #4b5563; font-size: 14px; margin-bottom: 18px;">
        Verificação automática em {timestamp}.<br>
        <span style="font-size: 12px; color: #6b7280;">
          🚨 Super Alerta: até {format_brl(super_alert_price)} · 🔥 Alerta: até {format_brl(alert_price)}
        </span>
      </p>
      {''.join(cards_html)}
      <p style="color: #9ca3af; font-size: 12px; margin-top: 20px;">
        Enviado automaticamente pelo seu Monitor de Preços PS5 (GitHub Actions).
      </p>
    </div>
    """
    return subject, "\n".join(plain_lines), html_body


def send_gmail(subject: str, plain_body: str, html_body: str) -> None:
    """Envia e-mail usando Gmail SMTP com Senha de App (sem expor dados nos logs)."""
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    email_to = os.environ.get("EMAIL_TO", "").strip() or gmail_user

    if not gmail_user or not gmail_password:
        raise RuntimeError(
            "Variáveis GMAIL_USER e GMAIL_APP_PASSWORD não configuradas. "
            "Configure-as nos Secrets do GitHub (ou no ambiente local)."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Monitor PS5 <{gmail_user}>"
    msg["To"] = email_to
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = [addr.strip() for addr in email_to.split(",") if addr.strip()]

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, recipients, msg.as_string())
    except OSError:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.starttls()
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, recipients, msg.as_string())

    # Nunca imprime endereços de e-mail para proteger a privacidade em logs públicos
    print("[OK] E-mail de alerta enviado com sucesso.")


def collect_all_offers(config: dict[str, Any]) -> list[Offer]:
    sources = config.get("sources", {})
    raw_offers: list[Offer] = []

    zoom_cfg = sources.get("zoom", {})
    if zoom_cfg.get("enabled", True):
        raw_offers.extend(
            fetch_mosaico_search(
                "Zoom",
                "https://www.zoom.com.br",
                zoom_cfg.get("queries", ["playstation 5 slim"]),
            )
        )

    buscape_cfg = sources.get("buscape", {})
    if buscape_cfg.get("enabled", True):
        raw_offers.extend(
            fetch_mosaico_search(
                "Buscapé",
                "https://www.buscape.com.br",
                buscape_cfg.get("queries", ["playstation 5 slim"]),
            )
        )

    kabum_cfg = sources.get("kabum", {})
    if kabum_cfg.get("enabled", True):
        raw_offers.extend(
            fetch_kabum_search(kabum_cfg.get("queries", ["playstation-5-slim"]))
        )

    custom_urls = sources.get("custom_urls", [])
    if custom_urls:
        raw_offers.extend(fetch_custom_urls(custom_urls))

    return deduplicate_offers(raw_offers)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor gratuito de preços de PS5.")
    parser.add_argument("--config", default="config.json", help="Caminho do arquivo config.json")
    parser.add_argument("--state", default="state.json", help="Caminho do arquivo state.json")
    parser.add_argument("--dry-run", action="store_true", help="Apenas exibe os resultados sem enviar e-mail")
    parser.add_argument("--test-email", action="store_true", help="Envia um e-mail de teste e encerra")
    parser.add_argument("--alert-price", "--max-price", dest="alert_price", type=float, default=None, help="Sobrescreve o preço de Alerta do config.json")
    parser.add_argument("--super-alert-price", type=float, default=None, help="Sobrescreve o preço de Super Alerta do config.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    alert_price = (
        args.alert_price
        if args.alert_price is not None
        else float(config.get("alert_price", config.get("max_price", 3500.0)))
    )
    super_alert_price = (
        args.super_alert_price
        if args.super_alert_price is not None
        else float(config.get("super_alert_price", 3000.0))
    )
    min_price = float(config.get("min_price", 1800.0))
    allowed_editions = set(config.get("allowed_editions", ["disc", "digital"]))
    min_drop = float(config.get("min_price_drop_to_renotify", 50.0))
    renotify_hours = float(config.get("renotify_after_hours", 24))

    now = datetime.now(timezone.utc)

    if args.test_email:
        subject, plain, html_body = build_email_content(
            [], alert_price, super_alert_price, now, is_test=True
        )
        send_gmail(subject, plain, html_body)
        return 0

    print(
        f"Iniciando monitoramento de PS5 "
        f"(🚨 Super Alerta: <= {format_brl(super_alert_price)} | "
        f"🔥 Alerta: <= {format_brl(alert_price)} | "
        f"mínimo: {format_brl(min_price)} | edições: {', '.join(sorted(allowed_editions))})"
    )

    all_offers = collect_all_offers(config)
    valid_consoles: list[Offer] = []
    for offer in all_offers:
        ok, _ = is_valid_ps5_console(offer.title, offer.price, min_price, allowed_editions)
        if ok:
            valid_consoles.append(offer)

    print(f"\nConsoles PS5 válidos encontrados: {len(valid_consoles)}")
    for item in valid_consoles[:10]:
        tier = classify_alert_tier(item.price, alert_price, super_alert_price)
        if tier == "super":
            marker = "🚨 SUPER!"
        elif tier == "alert":
            marker = "🔥 ALERTA"
        else:
            marker = "         "
        print(f"  {marker} {format_brl(item.price):>12s} | {item.edition_label:19s} | {item.source:8s} | {item.title}")

    deals = [o for o in valid_consoles if o.price <= alert_price]
    if not deals:
        print(f"\nNenhuma oferta abaixo de {format_brl(alert_price)} no momento.")
        return 0

    state_path = Path(args.state)
    state = load_state(state_path)
    to_notify = select_offers_to_notify(
        deals, state, alert_price, super_alert_price, min_drop, renotify_hours, now
    )

    if not to_notify:
        print(f"\n{len(deals)} oferta(s) dentro da meta, mas todas já foram notificadas recentemente.")
        return 0

    print(f"\n{len(to_notify)} nova(s) oferta(s) para notificar por e-mail!")
    subject, plain, html_body = build_email_content(
        to_notify, alert_price, super_alert_price, now
    )

    if args.dry_run:
        print(f"\n[DRY-RUN] Assunto: {subject}\n{plain}")
        return 0

    send_gmail(subject, plain, html_body)

    for deal in to_notify:
        state[deal.canonical_url] = {
            "title": deal.title,
            "price": deal.price,
            "tier": classify_alert_tier(deal.price, alert_price, super_alert_price),
            "source": deal.source,
            "notified_at": now.isoformat(),
        }
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
