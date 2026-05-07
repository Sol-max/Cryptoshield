"""
On-chain трекер связанных адресов.
Для каждого скам-адреса находит все адреса куда уходили деньги
и помечает их как подозрительные (с пониженным score).

Использует бесплатные публичные API:
- Etherscan (ETH/BSC)
- Blockstream (BTC)
- Tronscan (TRX)
- TON Center (TON)
- Solscan (SOL)
"""

import asyncio
import logging
import aiohttp
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Задержка между запросами чтобы не получить бан (сек)
REQUEST_DELAY = 0.3
# Глубина трекинга (сколько "прыжков" делаем)
MAX_DEPTH = 2
# Максимум адресов на один исходный (чтобы не уйти в бесконечность)
MAX_RELATED_PER_ADDRESS = 20


@dataclass
class RelatedAddress:
    address: str
    network: str
    depth: int           # 1 = прямой получатель, 2 = получатель получателя
    total_received: float  # сколько крипты получил от скам-адреса
    tx_count: int        # сколько транзакций связано
    risk_score: int      # рассчитанный score (ниже чем у родителя)
    reason: str          # почему помечен


class ChainTracer:
    def __init__(self, etherscan_api_key: str = None):
        # Etherscan даёт 5 req/sec бесплатно без ключа, с ключом — больше
        # Ключ получить: https://etherscan.io/myapikey
        self.etherscan_key = etherscan_api_key or "YourApiKeyToken"
        self.session: aiohttp.ClientSession = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _get(self, url: str, params: dict = None) -> dict | None:
        """Безопасный GET-запрос с обработкой ошибок."""
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
        except asyncio.TimeoutError:
            logger.warning(f"Timeout: {url}")
        except Exception as e:
            logger.warning(f"Request error {url}: {e}")
        return None

    # ─── Ethereum / BSC ──────────────────────────────────────────────────────

    async def trace_ethereum(self, address: str, depth: int = 1) -> list[RelatedAddress]:
        """Находит все адреса куда мошенник отправлял ETH."""
        url = "https://api.etherscan.io/api"
        params = {
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": 0,
            "endblock": 99999999,
            "sort": "desc",
            "offset": 100,
            "page": 1,
            "apikey": self.etherscan_key,
        }

        data = await self._get(url, params)
        await asyncio.sleep(REQUEST_DELAY)

        if not data or data.get("status") != "1":
            return []

        txs = data.get("result", [])
        # Только исходящие транзакции (мошенник отправляет)
        outgoing = [tx for tx in txs if tx.get("from", "").lower() == address.lower()]

        # Группируем по получателю
        recipients: dict[str, dict] = {}
        for tx in outgoing:
            to = tx.get("to", "").lower()
            if not to:
                continue
            value_eth = int(tx.get("value", 0)) / 1e18
            if to not in recipients:
                recipients[to] = {"total": 0.0, "count": 0}
            recipients[to]["total"] += value_eth
            recipients[to]["count"] += 1

        results = []
        for addr, info in list(recipients.items())[:MAX_RELATED_PER_ADDRESS]:
            # Чем больше получил и чем меньше прыжков — тем выше score
            base_score = 70 if depth == 1 else 45
            volume_bonus = min(20, int(info["total"] * 2))
            score = min(90, base_score + volume_bonus)

            results.append(RelatedAddress(
                address=addr,
                network="Ethereum/BSC",
                depth=depth,
                total_received=round(info["total"], 6),
                tx_count=info["count"],
                risk_score=score,
                reason=f"Получил {info['total']:.4f} ETH от скам-адреса (глубина {depth})"
            ))

        return results

    async def trace_ethereum_erc20(self, address: str, depth: int = 1) -> list[RelatedAddress]:
        """Находит переводы ERC-20 токенов (USDT, USDC и т.д.)."""
        url = "https://api.etherscan.io/api"
        params = {
            "module": "account",
            "action": "tokentx",
            "address": address,
            "sort": "desc",
            "offset": 100,
            "page": 1,
            "apikey": self.etherscan_key,
        }

        data = await self._get(url, params)
        await asyncio.sleep(REQUEST_DELAY)

        if not data or data.get("status") != "1":
            return []

        txs = data.get("result", [])
        outgoing = [tx for tx in txs if tx.get("from", "").lower() == address.lower()]

        recipients: dict[str, dict] = {}
        for tx in outgoing:
            to = tx.get("to", "").lower()
            token = tx.get("tokenSymbol", "?")
            decimals = int(tx.get("tokenDecimal", 18))
            value = int(tx.get("value", 0)) / (10 ** decimals)
            key = to
            if key not in recipients:
                recipients[key] = {"total": 0.0, "count": 0, "token": token}
            recipients[key]["total"] += value
            recipients[key]["count"] += 1

        results = []
        for addr, info in list(recipients.items())[:MAX_RELATED_PER_ADDRESS]:
            # USDT/USDC переводы — высокий приоритет
            score = 75 if depth == 1 else 50
            results.append(RelatedAddress(
                address=addr,
                network="Ethereum/BSC",
                depth=depth,
                total_received=round(info["total"], 2),
                tx_count=info["count"],
                risk_score=score,
                reason=f"Получил {info['total']:.2f} {info['token']} от скам-адреса (глубина {depth})"
            ))

        return results

    # ─── Bitcoin ─────────────────────────────────────────────────────────────

    async def trace_bitcoin(self, address: str, depth: int = 1) -> list[RelatedAddress]:
        """Находит исходящие BTC-транзакции."""
        url = f"https://blockstream.info/api/address/{address}/txs"
        data = await self._get(url)
        await asyncio.sleep(REQUEST_DELAY)

        if not data:
            return []

        recipients: dict[str, dict] = {}

        for tx in data[:50]:  # последние 50 транзакций
            # Проверяем что наш адрес — отправитель (есть в inputs)
            is_sender = any(
                inp.get("prevout", {}).get("scriptpubkey_address") == address
                for inp in tx.get("vin", [])
            )
            if not is_sender:
                continue

            for out in tx.get("vout", []):
                to = out.get("scriptpubkey_address")
                if not to or to == address:
                    continue
                value_btc = out.get("value", 0) / 1e8
                if to not in recipients:
                    recipients[to] = {"total": 0.0, "count": 0}
                recipients[to]["total"] += value_btc
                recipients[to]["count"] += 1

        results = []
        for addr, info in list(recipients.items())[:MAX_RELATED_PER_ADDRESS]:
            score = 70 if depth == 1 else 45
            results.append(RelatedAddress(
                address=addr,
                network="Bitcoin",
                depth=depth,
                total_received=round(info["total"], 8),
                tx_count=info["count"],
                risk_score=score,
                reason=f"Получил {info['total']:.6f} BTC от скам-адреса (глубина {depth})"
            ))

        return results

    # ─── Tron ─────────────────────────────────────────────────────────────────

    async def trace_tron(self, address: str, depth: int = 1) -> list[RelatedAddress]:
        """Находит TRX и USDT переводы в сети Tron."""
        url = f"https://apilist.tronscan.org/api/transaction"
        params = {
            "address": address,
            "direction": 1,   # 1 = исходящие
            "count": 50,
            "start": 0,
        }

        data = await self._get(url, params)
        await asyncio.sleep(REQUEST_DELAY)

        if not data:
            return []

        recipients: dict[str, dict] = {}

        for tx in data.get("data", []):
            to = tx.get("toAddress")
            if not to:
                continue
            amount = tx.get("amount", 0) / 1e6  # TRX в sunах
            if to not in recipients:
                recipients[to] = {"total": 0.0, "count": 0}
            recipients[to]["total"] += amount
            recipients[to]["count"] += 1

        results = []
        for addr, info in list(recipients.items())[:MAX_RELATED_PER_ADDRESS]:
            score = 70 if depth == 1 else 45
            results.append(RelatedAddress(
                address=addr,
                network="Tron",
                depth=depth,
                total_received=round(info["total"], 2),
                tx_count=info["count"],
                risk_score=score,
                reason=f"Получил {info['total']:.2f} TRX от скам-адреса (глубина {depth})"
            ))

        return results

    # ─── TON ─────────────────────────────────────────────────────────────────

    async def trace_ton(self, address: str, depth: int = 1) -> list[RelatedAddress]:
        """Находит исходящие транзакции в сети TON."""
        url = f"https://toncenter.com/api/v2/getTransactions"
        params = {
            "address": address,
            "limit": 50,
            "archival": False,
        }

        data = await self._get(url, params)
        await asyncio.sleep(REQUEST_DELAY)

        if not data or not data.get("ok"):
            return []

        recipients: dict[str, dict] = {}

        for tx in data.get("result", []):
            out_msgs = tx.get("out_msgs", [])
            for msg in out_msgs:
                to = msg.get("destination")
                if not to:
                    continue
                value_ton = int(msg.get("value", 0)) / 1e9
                if to not in recipients:
                    recipients[to] = {"total": 0.0, "count": 0}
                recipients[to]["total"] += value_ton
                recipients[to]["count"] += 1

        results = []
        for addr, info in list(recipients.items())[:MAX_RELATED_PER_ADDRESS]:
            score = 70 if depth == 1 else 45
            results.append(RelatedAddress(
                address=addr,
                network="TON",
                depth=depth,
                total_received=round(info["total"], 4),
                tx_count=info["count"],
                risk_score=score,
                reason=f"Получил {info['total']:.4f} TON от скам-адреса (глубина {depth})"
            ))

        return results

    # ─── Главный метод ────────────────────────────────────────────────────────

    async def trace_all(self, address: str, network: str) -> list[RelatedAddress]:
        """
        Полный трекинг адреса:
        1. Находим прямых получателей (depth=1)
        2. Для каждого получателя с высоким объёмом — повторяем (depth=2)
        """
        depth1 = await self._trace_by_network(address, network, depth=1)

        # Для depth=2 берём только адреса с высоким объёмом (топ-3)
        # чтобы не делать 20+ запросов
        high_volume = sorted(depth1, key=lambda x: x.total_received, reverse=True)[:3]

        depth2 = []
        if MAX_DEPTH >= 2:
            for related in high_volume:
                sub = await self._trace_by_network(related.address, network, depth=2)
                depth2.extend(sub)
                await asyncio.sleep(REQUEST_DELAY)

        all_results = depth1 + depth2

        # Убираем дубликаты (один адрес может появиться на обоих уровнях)
        seen = set()
        unique = []
        for r in all_results:
            if r.address.lower() not in seen:
                seen.add(r.address.lower())
                unique.append(r)

        return unique

    async def _trace_by_network(self, address: str, network: str, depth: int) -> list[RelatedAddress]:
        """Выбирает правильный трекер по сети."""
        net = network.lower()
        if "bitcoin" in net or "btc" in net:
            return await self.trace_bitcoin(address, depth)
        elif "ethereum" in net or "eth" in net or "bsc" in net or "polygon" in net:
            eth = await self.trace_ethereum(address, depth)
            erc20 = await self.trace_ethereum_erc20(address, depth)
            # Объединяем и дедуплицируем
            combined = {r.address.lower(): r for r in eth}
            for r in erc20:
                key = r.address.lower()
                if key in combined:
                    # Берём максимальный score
                    if r.risk_score > combined[key].risk_score:
                        combined[key] = r
                else:
                    combined[key] = r
            return list(combined.values())
        elif "tron" in net or "trx" in net:
            return await self.trace_tron(address, depth)
        elif "ton" in net:
            return await self.trace_ton(address, depth)
        else:
            logger.warning(f"Неизвестная сеть для трекинга: {network}")
            return []
