import re
import json
import logging
import anthropic # Важно: оставить этот импорт

logger = logging.getLogger(__name__)

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
        # Используем АСИНХРОННЫЙ клиент
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    def _quick_extract(self, text: str) -> list[str]:
        found = []
        for network, pattern in CRYPTO_PATTERNS.items():
            matches = re.findall(pattern, text)
            for m in matches:
                if network == "solana" and len(m) < 32: continue
                if m not in found: found.append(m)
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
        elif 32 <= len(address) <= 44:
            return "Solana"
        return "Unknown"

    async def analyze(self, text: str, user_id: int, username: str) -> dict:
        quick_addresses = self._quick_extract(text)

        prompt = f"""Проанализируй это сообщение и определи является ли оно крипто-мошенничеством.
        СООБЩЕНИЕ: {text[:3000]}
        ЗАДАЧА:
        1. Найди ВСЕ крипто-адреса.
        2. Определи тип мошенничества. Регулярки нашли: {quick_addresses}
        Типы: {', '.join(SCAM_TYPES)}
        Ответь ТОЛЬКО в JSON формате:
        {{
          "is_scam": true,
          "confidence": 95,
          "scam_type": "название",
          "addresses": [{{"address": "...", "network": "...", "confidence": 100}}],
          "summary": "резюме на русском",
          "red_flags": []
        }}"""

        try:
            # ДОБАВЛЕН await и ИСПРАВЛЕНА модель
            response = await self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )

            raw = response.content[0].text.strip()
            raw = re.sub(r'```json\s*|\s*```', '', raw) # Более надежная очистка JSON
            ai_result = json.loads(raw)

        except Exception as e:
            logger.error(f"Ошибка Claude API: {e}")
            # Fallback на регулярки если AI упал
            ai_result = {
                "is_scam": len(quick_addresses) > 0,
                "confidence": 50,
                "scam_type": "Анализ AI временно недоступен",
                "addresses": [{"address": a, "network": self._detect_network(a), "confidence": 70} for a in quick_addresses],
                "summary": f"Ошибка AI: {str(e)}",
                "red_flags": []
            }

        return ai_result
