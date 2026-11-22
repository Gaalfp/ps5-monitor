import os
import smtplib
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import monitor


class TestPS5Monitor(unittest.TestCase):
    def test_parse_brl_price(self):
        self.assertEqual(monitor.parse_brl_price("R$ 3.299,90"), 3299.90)
        self.assertEqual(monitor.parse_brl_price("R$ 4.530,55"), 4530.55)
        self.assertEqual(monitor.parse_brl_price(3199), 3199.0)
        self.assertEqual(monitor.parse_brl_price("2999.50"), 2999.50)
        self.assertIsNone(monitor.parse_brl_price("Indisponível"))

    def test_console_filtering_accepts_real_consoles(self):
        allowed = {"disc", "digital"}
        ok_digital, ed1 = monitor.is_valid_ps5_console(
            "Console Sony PlayStation 5 Edição Digital Slim, SSD 825GB, Controle DualSense, Branco",
            3199.0,
            1800.0,
            allowed,
        )
        self.assertTrue(ok_digital)
        self.assertEqual(ed1, "digital")

        ok_disc, ed2 = monitor.is_valid_ps5_console(
            "Playstation 5 Slim 1TB Com Leitor de Disco",
            3299.0,
            1800.0,
            allowed,
        )
        self.assertTrue(ok_disc)
        self.assertEqual(ed2, "disc")

    def test_console_filtering_rejects_accessories_and_pro(self):
        allowed = {"disc", "digital"}
        rejected_cases = [
            ("Leitor de Disco Para Playstation 5 Slim, PS5 Pro Sony", 2500.0),
            ("Tampa para Console PlayStation 5 Slim, Azul", 2000.0),
            ("Console PlayStation 5 Pro 2TB SSD Sony", 3200.0),
            ("PlayStation Portal Reprodutor Remoto para PS5", 2000.0),
            ("PlayStation 5 Slim Usado Sem Caixa", 2600.0),
            ("Controle Sem Fio DualSense PS5", 450.0),
            ("Console PlayStation 5 Slim", 999.0),
        ]
        for title, price in rejected_cases:
            ok, _ = monitor.is_valid_ps5_console(title, price, 1800.0, allowed)
            self.assertFalse(ok, f"Deveria rejeitar: {title}")

    def test_deduplicate_zoom_and_buscape(self):
        offers = [
            monitor.Offer("Zoom", "Playstation 5 Slim 1TB", 3199.0, "https://www.zoom.com.br/p/1?_lc=88"),
            monitor.Offer("Buscapé", "Playstation 5 Slim 1TB", 3199.0, "https://www.buscape.com.br/p/1?_lc=88"),
            monitor.Offer("KaBuM!", "Console PS5 Slim Digital", 3099.0, "https://www.kabum.com.br/produto/1/ps5"),
        ]
        deduped = monitor.deduplicate_offers(offers)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0].source, "KaBuM!")
        self.assertEqual(deduped[1].source, "Zoom")

    def test_alert_tiers_and_anti_spam(self):
        now = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)
        alert_price, super_price = 3500.0, 3000.0

        self.assertEqual(monitor.classify_alert_tier(2999.0, alert_price, super_price), "super")
        self.assertEqual(monitor.classify_alert_tier(3300.0, alert_price, super_price), "alert")
        self.assertIsNone(monitor.classify_alert_tier(3600.0, alert_price, super_price))

        offer = monitor.Offer("Zoom", "PS5 Slim 1TB", 3300.0, "https://www.zoom.com.br/p/ps5")

        # 1. Primeira vez como Alerta normal: deve notificar
        res = monitor.select_offers_to_notify([offer], {}, alert_price, super_price, 50.0, 24.0, now)
        self.assertEqual(len(res), 1)

        # 2. Já notificado há 2h pelo mesmo preço: NÃO notifica
        state = {
            offer.canonical_url: {
                "price": 3300.0,
                "tier": "alert",
                "notified_at": (now - timedelta(hours=2)).isoformat(),
            }
        }
        res = monitor.select_offers_to_notify([offer], state, alert_price, super_price, 50.0, 24.0, now)
        self.assertEqual(len(res), 0)

        # 3. Caiu de 3020 (alert) para 2999 (super): mesmo caindo só R$ 21, DEVE notificar pois virou SUPER ALERTA!
        state_near = {
            offer.canonical_url: {
                "price": 3020.0,
                "tier": "alert",
                "notified_at": (now - timedelta(minutes=10)).isoformat(),
            }
        }
        super_offer = monitor.Offer("Zoom", "PS5 Slim 1TB", 2999.0, "https://www.zoom.com.br/p/ps5")
        res = monitor.select_offers_to_notify([super_offer], state_near, alert_price, super_price, 50.0, 24.0, now)
        self.assertEqual(len(res), 1)

        # 4. Caiu R$ 100 dentro do mesmo tier: DEVE notificar
        cheaper = monitor.Offer("Zoom", "PS5 Slim 1TB", 3200.0, "https://www.zoom.com.br/p/ps5")
        res = monitor.select_offers_to_notify([cheaper], state, alert_price, super_price, 50.0, 24.0, now)
        self.assertEqual(len(res), 1)

        # 5. Passaram 25h (cooldown expirou): DEVE notificar
        old_state = {
            offer.canonical_url: {
                "price": 3300.0,
                "tier": "alert",
                "notified_at": (now - timedelta(hours=25)).isoformat(),
            }
        }
        res = monitor.select_offers_to_notify([offer], old_state, alert_price, super_price, 50.0, 24.0, now)
        self.assertEqual(len(res), 1)

    def test_email_subjects_for_tiers(self):
        now = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)
        alert_offer = monitor.Offer("Zoom", "PS5 Slim 1TB", 3300.0, "https://www.zoom.com.br/p/ps5", edition="disc")
        subj_alert, _, _ = monitor.build_email_content([alert_offer], 3500.0, 3000.0, now)
        self.assertIn("Alerta PS5", subj_alert)
        self.assertIn("R$ 3.300,00", subj_alert)

        super_offer = monitor.Offer("KaBuM!", "PS5 Digital", 2949.90, "https://www.kabum.com.br/p/1", edition="digital")
        subj_super, _, _ = monitor.build_email_content([super_offer, alert_offer], 3500.0, 3000.0, now)
        self.assertIn("SUPER ALERTA", subj_super)
        self.assertIn("R$ 2.949,90", subj_super)

    def test_parse_mosaico_and_kabum_html(self):
        zoom_html = """
        <a data-testid="product-card::card" href="/console/ps5-slim?_lc=88&searchterm=ps5">
          <h2 data-testid="product-card::name">Playstation 5 Slim 1TB</h2>
          <p data-testid="product-card::price"><strong>R$ 3.199,00</strong></p>
        </a>
        """
        offers = monitor.parse_mosaico_cards(zoom_html, "Zoom", "https://www.zoom.com.br")
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].price, 3199.0)
        self.assertEqual(offers[0].url, "https://www.zoom.com.br/console/ps5-slim")

        kabum_html = """
        <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"data":"{\\"catalogServer\\":{\\"data\\":[{\\"code\\":1050570,\\"name\\":\\"Console Sony PlayStation 5 Edição Digital Slim\\",\\"price\\":3499,\\"priceWithDiscount\\":3150.5,\\"friendlyName\\":\\"ps5-digital\\",\\"available\\":true,\\"sellerName\\":\\"KaBuM!\\"}]}}"}}}
        </script>
        """
        k_offers = monitor.parse_kabum_page(kabum_html)
        self.assertEqual(len(k_offers), 1)
        self.assertEqual(k_offers[0].price, 3150.5)
        self.assertEqual(k_offers[0].edition, "digital")

    def test_send_gmail_sanitizes_smtp_errors(self):
        fake_env = {
            "GMAIL_USER": "secreto1@example.com",
            "GMAIL_APP_PASSWORD": "abcdefghijklmnop",
            "EMAIL_TO": "secreto1@example.com, secreto2@example.com",
        }
        err = smtplib.SMTPRecipientsRefused({"secreto2@example.com": (550, b"User unknown")})
        with mock.patch.dict(os.environ, fake_env, clear=False):
            with mock.patch("smtplib.SMTP_SSL") as mock_ssl:
                mock_ssl.return_value.__enter__.return_value.sendmail.side_effect = err
                with self.assertRaises(RuntimeError) as ctx:
                    monitor.send_gmail("Assunto", "Texto", "<p>HTML</p>")
                msg = str(ctx.exception)
                self.assertNotIn("secreto1@example.com", msg)
                self.assertNotIn("secreto2@example.com", msg)
                self.assertNotIn("abcdefghijklmnop", msg)


if __name__ == "__main__":
    unittest.main()
