"""
Анализатор скам-сообщений через Claude AI.
"""

import re
import json
import logging
import anthropic

logger = logging.getLogger(__name__)

# Регулярки для быстрого pre-поиска адресов
CRYPTO_PATTERNS = {
    "bitcoin": r"\b(1[a-km-zA-HJ-NP-Z1-9]{25,34}|3[a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{39,59})\b",
    "ethereum": r"\b0x[a-fA-F0-9]{40}\b",
    "tron": r"\bT[a-km-zA-HJ-NP-Z1-9]{33}\b",
    "ton": r"\b(EQ|UQ)[a-zA-Z0-9_-]{46}\b",
    "solana": r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b",
}

SCAM_TYPES = [
    "романтический скам (pig butchering)",
    "поддельная инвест-платформа",
    "фишинг / поддельный сайт",
    "rug pull / поддельный токен",
    "fake airdrop / раздача",
    "скам техподдержки",
    "pump and dump",
    "другой тип мошенничества",
]


class ScamAnalyzer:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def _quick_extract(self, text: str) -> list[str]:
        """Быстрое извлечение адресов регулярками (без AI)."""
        found = []
        for network, pattern in CRYPTO_PATTERNS.items():
            matches = re.findall(pattern, text)
            for m in matches:
                # Фильтруем слишком короткие совпадения для Solana
                if network == "solana" and len(m) < 32:
                    continue
                if m not in found:
                    found.append(m)
        return found

    def _detect_network(self, address: str) -> str:
        if re.match(r"^(1[a-km-zA-HJ-NP-Z1-9]{25,34}|3[a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{39,59})$", address):
            return "Bitcoin"
        elif re.match(r"^0x[a-fA-F0-9]{40}$", address):
            return "Ethereum/BSC"
        elif re.match(r"^T[a-km-zA-HJ-NP-Z1-9]{33}$", address):
            return "Tron"
        elif re.match(r"^(EQ|UQ)[a-zA-Z0-9_-]{46}$", address):
            return "TON"
        elif len(address) in range(32, 45):
            return "Solana"
        return "Unknown"

    async def analyze(self, text: str, user_id: int, username: str) -> dict:
        """
        Полный AI-анализ сообщения.
        Возвращает: addresses, scam_type, summary, confidence
        """

        # Шаг 1: быстрое извлечение регулярками
        quick_addresses = self._quick_extract(text)

        # Шаг 2: AI-анализ через Claude
        prompt = f"""Проанализируй это сообщение и определи является ли оно крипто-мошенничеством.

СООБЩЕНИЕ:
{text[:3000]}

ЗАДАЧА:
1. Найди ВСЕ крипто-адреса (Bitcoin, Ethereum, Tron, TON, Solana и другие)
2. Определи тип мошенничества
3. Напиши короткое резюме (1-2 предложения) на русском

Регулярки уже нашли эти адреса: {quick_addresses}
Проверь их и найди дополнительные если есть.

Типы мошенничества: {', '.join(SCAM_TYPES)}

Ответь ТОЛЬКО в JSON формате без markdown:
{{
  "is_scam": true/false,
  "confidence": 0-100,
  "scam_type": "тип или null",
  "addresses": [
    {{"address": "адрес", "network": "сеть", "confidence": 0-100}}
  ],
  "summary": "краткое резюме на русском",
  "red_flags": ["флаг1", "флаг2"]
}}"""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )

            raw = response.content[0].text.strip()
            # Убираем markdown если вдруг появился
            raw = raw.replace("```json", "").replace("```", "").strip()
            ai_result = json.loads(raw)

        except json.JSONDecodeError as e:
            logger.warning(f"AI вернул не-JSON: {e}. Используем regex-результаты.")
            ai_result = {
                "is_scam": len(quick_addresses) > 0,
                "confidence": 50,
                "scam_type": None,
                "addresses": [
                    {"address": a, "network": self._detect_network(a), "confidence": 70}
                    for a in quick_addresses
                ],
                "summary": "Найдены крипто-адреса (AI-анализ недоступен)",
                "red_flags": []
            }
        except Exception as e:
            logger.error(f"Ошибка Claude API: {e}")
            raise

        # Дополняем данными о сети если AI не заполнил
        for addr_info in ai_result.get("addresses", []):
            if not addr_info.get("network") or addr_info["network"] == "Unknown":
                addr_info["network"] = self._detect_network(addr_info["address"])

        return ai_result