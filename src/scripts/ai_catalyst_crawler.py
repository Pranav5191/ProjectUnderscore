"""
src/scripts/ai_catalyst_crawler.py
Zero-Dependency Concurrent 7-Feed Catalyst Crawler:
  1. NSE Corporate Announcements (JSON API)
  2. Official NSE Announcements (XML RSS)
  3. BSE Corporate Announcements (JSON API)
  4. HFT-Filtered Bulk & Block Deals
  5. Today's Scheduled Board Meetings
  6. SEBI Insider Trading / SAST Filings
  7. Google News RSS (Targeted Equities)
"""

import io
import re
import time
import logging
import requests
import xml.etree.ElementTree as ET
from pypdf import PdfReader
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Set, Tuple, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("AICatalystCrawler")
IST = ZoneInfo("Asia/Kolkata")

HIGH_IMPACT_KEYWORDS = re.compile(
    r"(order\s+win|bagged|contract|letter\s+of\s+award|loa|demerger|acqui|"
    r"merger|buyback|sebi|resign|cfo|ceo|auditor|default|fraud|raid|search|"
    r"financial\s+results|outcome\s+of\s+board|dividend|bonus|split|rights\s+issue|"
    r"fda|eir|usfda|approval|joint\s+venture|mou|settlement|arbitration|insolvency|nclt|"
    r"bulk_deal|block_deal|board_meeting|insider_pit|pledge|preferential|qip)",
    re.IGNORECASE
)

LOW_NOISE_EXCLUSIONS = re.compile(
    r"(loss\s+of\s+share\s+certificate|duplicate\s+share|closure\s+of\s+trading\s+window|"
    r"newspaper\s+publication|investor\s+grievance|compliance\s+certificate|esop\s+allotment|"
    r"postal\s+ballot\s+scrutinizer|change\s+in\s+rta)",
    re.IGNORECASE
)

HFT_JOBBER_FIRMS = re.compile(
    r"(graviton|nk\s+securities|alphagrep|qe\s+securities|jump\s+trading|"
    r"microcurves|irage\s+broking|hrti\s+private|junomoneta|mathisys|imc\s+india|"
    r"elixir\s+wealth|pace\s+stock\s+broking|patronus\s+tradetech|akg\s+securities|"
    r"citadel\s+securities|tower\s+research|yuga\s+stocks|mansi\s+share)",
    re.IGNORECASE
)


class CatalystCrawler:
    NSE_BASE_URL = "https://www.nseindia.com"
    NSE_EQUITIES_ANN_API = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    NSE_RSS_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
    NSE_LARGE_DEALS_API = "https://www.nseindia.com/api/snapshot-capital-market-largedeals"
    NSE_BOARD_MEETINGS_API = "https://www.nseindia.com/api/corporate-board-meetings?index=equities"
    NSE_INSIDER_PIT_API = "https://www.nseindia.com/api/corporates-pit?index=equities"
    BSE_ANN_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"

    def __init__(self, timeout_sec: int = 15):
        self.timeout = timeout_sec
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }

    @staticmethod
    def get_ist_window(now_ist: Optional[datetime] = None) -> Tuple[datetime, datetime]:
        if now_ist is None:
            now_ist = datetime.now(IST)

        today_date = now_ist.date()
        if today_date.weekday() == 0:
            prev_mkt_date = today_date - timedelta(days=3)
        elif today_date.weekday() == 6:
            prev_mkt_date = today_date - timedelta(days=2)
        else:
            prev_mkt_date = today_date - timedelta(days=1)

        start_dt = datetime.combine(prev_mkt_date, dtime(15, 30, 0), tzinfo=IST)
        end_dt = datetime.combine(today_date, dtime(8, 35, 0), tzinfo=IST)

        if now_ist > end_dt:
            end_dt = now_ist

        return start_dt, end_dt

    @staticmethod
    def normalize_symbol(raw_sym: str) -> str:
        clean = str(raw_sym or "").strip().upper()
        if not clean:
            return ""
        return clean if clean.endswith("-EQ") else f"{clean}-EQ"

    @staticmethod
    def parse_timestamp(raw_dt_str: str) -> Optional[datetime]:
        if not raw_dt_str:
            return None
        raw_dt_str = str(raw_dt_str).strip()
        formats = [
            "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT",
            "%d/%m/%Y %H:%M:%S",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(raw_dt_str.split('.')[0], fmt.split('.')[0])
                return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)
            except ValueError:
                continue
        return None

    def extract_pdf_summary(self, pdf_path: str, session: requests.Session) -> str:
        if not pdf_path:
            return ""
        pdf_url = pdf_path if pdf_path.startswith("http") else f"https://nsearchives.nseindia.com/corporate/{pdf_path.lstrip('/')}"
        try:
            resp = session.get(pdf_url, timeout=self.timeout)
            if resp.status_code == 200:
                pdf_file = io.BytesIO(resp.content)
                reader = PdfReader(pdf_file)
                extracted_text = ""
                for i in range(min(2, len(reader.pages))):
                    page_text = reader.pages[i].extract_text()
                    if page_text:
                        extracted_text += page_text + "\n"
                clean_text = re.sub(r'\s+', ' ', extracted_text).strip()
                if len(clean_text) < 50:
                    return "[SCANNED IMAGE PDF - SKIPPED]"
                return clean_text[:1200]
        except Exception as e:
            logger.debug(f"PDF extraction skipped for {pdf_url}: {e}")
        return ""

    def fetch_nse_json_announcements(self, start_dt: datetime, end_dt: datetime) -> List[Dict]:
        records = []
        session = requests.Session()
        session.headers.update(self.headers)
        session.headers["Referer"] = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"

        from_date_str = start_dt.strftime("%d-%m-%Y")
        to_date_str = end_dt.strftime("%d-%m-%Y")
        url = f"{self.NSE_EQUITIES_ANN_API}&from_date={from_date_str}&to_date={to_date_str}"

        try:
            for attempt in range(2):
                try:
                    session.get(self.NSE_BASE_URL, timeout=self.timeout)
                    resp = session.get(url, timeout=self.timeout)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list):
                            for item in data:
                                sym = self.normalize_symbol(item.get("symbol", ""))
                                dt = self.parse_timestamp(item.get("an_dt") or item.get("sort_date", ""))
                                subject = item.get("desc", "")
                                details = item.get("attchmntText", "")
                                attachment_path = item.get("attachment", "")
                                
                                if sym and dt and (start_dt <= dt <= end_dt):
                                    text_blob = f"{subject} — {details}"
                                    if attachment_path and HIGH_IMPACT_KEYWORDS.search(text_blob):
                                        pdf_text = self.extract_pdf_summary(attachment_path, session)
                                        if pdf_text and "[SCANNED IMAGE" not in pdf_text:
                                            details = f"{details} | PDF EXTRACT: {pdf_text}"

                                    records.append({
                                        "symbol": sym,
                                        "timestamp": dt.isoformat(),
                                        "subject": subject,
                                        "details": details,
                                        "source": "NSE_API"
                                    })
                        break
                    time.sleep(1.0)
                except Exception as e:
                    logger.warning(f"NSE API fetch attempt {attempt + 1} failed: {e}")
                    time.sleep(1.0)
        finally:
            session.close()
        return records

    def fetch_bse_json_announcements(self, start_dt: datetime, end_dt: datetime) -> List[Dict]:
        records = []
        session = requests.Session()
        session.headers.update(self.headers)
        session.headers["Referer"] = "https://www.bseindia.com/corporates/ann.html"

        params = {
            "pageno": "1", "strCat": "-1", "strPrevDate": start_dt.strftime("%Y%m%d"),
            "strScrip": "", "strSearch": "P", "strToDate": end_dt.strftime("%Y%m%d"),
            "strType": "C", "subcategory": ""
        }
        try:
            session.get("https://www.bseindia.com", timeout=self.timeout)
            resp = session.get(self.BSE_ANN_API, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                table = resp.json().get("Table", [])
                for item in table:
                    raw_sym = str(item.get("SLONGNAME", "")).split()[0].upper()
                    sym = self.normalize_symbol(raw_sym)
                    dt = self.parse_timestamp(item.get("NEWS_DT", ""))
                    subject = item.get("NEWSSUB", "")
                    details = item.get("HEADLINE", "")
                    attachment = item.get("ATTACHMENTNAME", "")
                    
                    if dt and (start_dt <= dt <= end_dt):
                        text_blob = f"{subject} — {details}"
                        if attachment and HIGH_IMPACT_KEYWORDS.search(text_blob):
                            pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}"
                            pdf_text = self.extract_pdf_summary(pdf_url, session)
                            if pdf_text and "[SCANNED IMAGE" not in pdf_text:
                                details = f"{details} | PDF EXTRACT: {pdf_text}"
                                
                        records.append({
                            "symbol": sym,
                            "timestamp": dt.isoformat(),
                            "subject": f"BSE: {subject}",
                            "details": details,
                            "source": "BSE_API"
                        })
        except Exception as e:
            logger.warning(f"BSE API fetch warning: {e}")
        finally:
            session.close()
        return records

    def fetch_google_news_rss(self, start_dt: datetime, end_dt: datetime, top_50_symbols: List[str]) -> List[Dict]:
        records = []
        if not top_50_symbols:
            return records
        
        targeted_syms = [s.replace('-EQ', '') for s in top_50_symbols[:15]]
        query = "+OR+".join(targeted_syms) + "+stock+NSE"
        url = f'https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en'
        
        try:
            resp = requests.get(url, headers=self.headers, timeout=self.timeout)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                for item in root.findall(".//item"):
                    title = (item.findtext("title") or "").strip()
                    pub_date = (item.findtext("pubDate") or "").strip()
                    dt = self.parse_timestamp(pub_date)
                    if dt and not (start_dt <= dt <= end_dt):
                        continue
                        
                    matched_sym = ""
                    for sym in targeted_syms:
                        if sym.lower() in title.lower():
                            matched_sym = f"{sym}-EQ"
                            break
                            
                    if matched_sym:
                        records.append({
                            "symbol": matched_sym,
                            "timestamp": dt.isoformat() if dt else datetime.now(IST).isoformat(),
                            "subject": f"GOOGLE NEWS: {title}",
                            "details": "Mainstream media news report.",
                            "source": "GOOGLE_NEWS"
                        })
        except Exception as e:
            logger.warning(f"Google News RSS fetch warning: {e}")
        return records

    def fetch_nse_rss_announcements(self, start_dt: datetime, end_dt: datetime) -> List[Dict]:
        records = []
        try:
            resp = requests.get(self.NSE_RSS_URL, headers=self.headers, timeout=self.timeout)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                for item in root.findall(".//item"):
                    title = (item.findtext("title") or "").strip()
                    desc = (item.findtext("description") or "").strip()
                    pub_date = (item.findtext("pubDate") or "").strip()
                    dt = self.parse_timestamp(pub_date)
                    if dt and not (start_dt <= dt <= end_dt):
                        continue
                    sym_match = re.match(r"^([A-Z0-9&-]{2,20})\s*[:\-]", title)
                    if sym_match:
                        sym = self.normalize_symbol(sym_match.group(1))
                        records.append({
                            "symbol": sym,
                            "timestamp": dt.isoformat() if dt else datetime.now(IST).isoformat(),
                            "subject": title,
                            "details": desc,
                            "source": "NSE_RSS"
                        })
        except Exception as e:
            logger.warning(f"NSE RSS fetch warning: {e}")
        return records

    def fetch_nse_bulk_block_deals(self) -> List[Dict]:
        records = []
        session = requests.Session()
        session.headers.update(self.headers)
        session.headers["Referer"] = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
        try:
            session.get(self.NSE_BASE_URL, timeout=self.timeout)
            resp = session.get(self.NSE_LARGE_DEALS_API, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                client_ledger: Dict[Tuple[str, str, str], Dict] = {}

                for deal_type, key in [("BULK_DEAL", "BULK_DEALS_DATA"), ("BLOCK_DEAL", "BLOCK_DEALS_DATA")]:
                    for item in data.get(key, []):
                        sym = self.normalize_symbol(item.get("symbol", ""))
                        client = str(item.get("clientName", "")).strip()
                        if not sym or not client:
                            continue

                        if deal_type == "BULK_DEAL" and HFT_JOBBER_FIRMS.search(client):
                            continue

                        action = str(item.get("buySell", "")).upper().strip()
                        try:
                            qty = int(str(item.get("quantity", "0")).replace(",", ""))
                            price = float(str(item.get("watp", "0")).replace(",", ""))
                        except ValueError:
                            continue

                        ledger_key = (sym, client, deal_type)
                        entry = client_ledger.setdefault(
                            ledger_key,
                            {"buy_qty": 0, "sell_qty": 0, "price": price, "date": str(item.get("date", "Overnight"))}
                        )
                        if action == "BUY":
                            entry["buy_qty"] += qty
                        elif action == "SELL":
                            entry["sell_qty"] += qty

                for (sym, client, deal_type), info in client_ledger.items():
                    total_q = info["buy_qty"] + info["sell_qty"]
                    net_q = info["buy_qty"] - info["sell_qty"]
                    if total_q == 0 or abs(net_q) / total_q < 0.70:
                        continue

                    direction = "NET INSTITUTIONAL BUY" if net_q > 0 else "NET INSTITUTIONAL SELL"
                    est_val_cr = round((abs(net_q) * info["price"]) / 1e7, 2)
                    if est_val_cr < 1.0:
                        continue

                    records.append({
                        "symbol": sym,
                        "timestamp": info["date"],
                        "subject": f"{deal_type} ({direction}): {client}",
                        "details": (
                            f"Client '{client}' recorded {direction} of {abs(net_q):,} shares "
                            f"at Rs.{info['price']} (Net Deal Value: ~Rs.{est_val_cr} Crore)."
                        ),
                        "source": deal_type
                    })
        except Exception as e:
            logger.warning(f"NSE Bulk/Block deals fetch warning: {e}")
        finally:
            session.close()
        return records

    def fetch_nse_board_meetings_today(self) -> List[Dict]:
        records = []
        session = requests.Session()
        session.headers.update(self.headers)
        session.headers["Referer"] = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
        today_str = datetime.now(IST).strftime("%d-%b-%Y")
        try:
            session.get(self.NSE_BASE_URL, timeout=self.timeout)
            resp = session.get(self.NSE_BOARD_MEETINGS_API, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    for item in data:
                        bm_date = str(item.get("bm_date", "")).strip()
                        if bm_date.lower() == today_str.lower():
                            sym = self.normalize_symbol(item.get("symbol", ""))
                            purpose = item.get("bm_purpose", "")
                            desc = item.get("bm_desc", "")
                            if sym:
                                records.append({
                                    "symbol": sym,
                                    "timestamp": bm_date,
                                    "subject": f"BOARD_MEETING TODAY: {purpose}",
                                    "details": f"Scheduled Board Meeting today ({bm_date}) for: {desc}",
                                    "source": "NSE_BOARD_MEETING"
                                })
        except Exception as e:
            logger.warning(f"NSE Board Meetings fetch warning: {e}")
        finally:
            session.close()
        return records

    def fetch_nse_insider_pit(self, start_dt: datetime, end_dt: datetime) -> List[Dict]:
        records = []
        session = requests.Session()
        session.headers.update(self.headers)
        session.headers["Referer"] = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
        try:
            session.get(self.NSE_BASE_URL, timeout=self.timeout)
            resp = session.get(self.NSE_INSIDER_PIT_API, timeout=self.timeout)
            if resp.status_code == 200:
                payload = resp.json()
                data = payload.get("data", []) if isinstance(payload, dict) else []
                for item in data:
                    sym = self.normalize_symbol(item.get("symbol", ""))
                    dt = self.parse_timestamp(item.get("date") or item.get("acqtoDt", ""))
                    if not sym or (dt and not (start_dt.date() <= dt.date() <= end_dt.date())):
                        continue

                    person_cat = str(item.get("personCategory", "")).strip()
                    mode = str(item.get("tdpTransactionType", "")).strip()
                    sec_val = str(item.get("secVal", "0")).replace(",", "")
                    acq_mode = str(item.get("acqMode", "")).strip()

                    try:
                        val_cr = round(float(sec_val) / 1e7, 2)
                    except ValueError:
                        val_cr = 0.0

                    if val_cr >= 0.50 and any(k in person_cat.lower() for k in ["promoter", "director", "key managerial"]):
                        records.append({
                            "symbol": sym,
                            "timestamp": dt.isoformat() if dt else "Overnight",
                            "subject": f"INSIDER_PIT ({person_cat}): {mode} via {acq_mode}",
                            "details": (
                                f"{person_cat} ('{item.get('acqName', '')}') reported {mode} ({acq_mode}) "
                                f"worth ~Rs.{val_cr} Crore."
                            ),
                            "source": "NSE_INSIDER_PIT"
                        })
        except Exception as e:
            logger.warning(f"NSE Insider PIT fetch warning: {e}")
        finally:
            session.close()
        return records

    def fetch_all_filings(
        self, top_50_symbols: List[str]
    ) -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]]]:
        start_dt, end_dt = self.get_ist_window()
        logger.info(
            f"Scanning filings window: {start_dt.strftime('%Y-%m-%d %H:%M')} "
            f"to {end_dt.strftime('%Y-%m-%d %H:%M')} IST"
        )

        top_50_set: Set[str] = {self.normalize_symbol(s) for s in top_50_symbols}
        math_top50_filings: Dict[str, List[Dict]] = {s: [] for s in top_50_set}
        broad_market_filings: Dict[str, List[Dict]] = {}

        with ThreadPoolExecutor(max_workers=7) as executor:
            f_api = executor.submit(self.fetch_nse_json_announcements, start_dt, end_dt)
            f_rss = executor.submit(self.fetch_nse_rss_announcements, start_dt, end_dt)
            f_deals = executor.submit(self.fetch_nse_bulk_block_deals)
            f_bm = executor.submit(self.fetch_nse_board_meetings_today)
            f_pit = executor.submit(self.fetch_nse_insider_pit, start_dt, end_dt)
            f_bse = executor.submit(self.fetch_bse_json_announcements, start_dt, end_dt)
            f_gnews = executor.submit(self.fetch_google_news_rss, start_dt, end_dt, top_50_symbols)

            res_api = f_api.result()
            res_rss = f_rss.result()
            res_deals = f_deals.result()
            res_bm = f_bm.result()
            res_pit = f_pit.result()
            res_bse = f_bse.result()
            res_gnews = f_gnews.result()

        logger.info(
            f"[7-FEED CRAWLER REPORT] "
            f"1.NSE_API: {len(res_api)} | 2.NSE_RSS: {len(res_rss)} | 3.DEALS: {len(res_deals)} | "
            f"4.BOARD_MEETINGS: {len(res_bm)} | 5.INSIDER_PIT: {len(res_pit)} | "
            f"6.BSE_API: {len(res_bse)} | 7.GOOGLE_NEWS: {len(res_gnews)}"
        )

        combined = res_api + res_rss + res_deals + res_bm + res_pit + res_bse + res_gnews
        seen_hashes: Set[str] = set()

        for rec in combined:
            sym = rec["symbol"]
            text_blob = f"{rec.get('subject', '')} — {rec.get('details', '')}".strip()
            dedup_key = f"{sym}:{text_blob[:120]}"
            if dedup_key in seen_hashes:
                continue
            seen_hashes.add(dedup_key)

            if LOW_NOISE_EXCLUSIONS.search(text_blob) and not HIGH_IMPACT_KEYWORDS.search(text_blob):
                continue

            if sym in top_50_set:
                math_top50_filings[sym].append(rec)
            else:
                if HIGH_IMPACT_KEYWORDS.search(text_blob):
                    broad_market_filings.setdefault(sym, []).append(rec)

        try:
            import json
            debug_dump = {
                "feed_counts": {
                    "1_NSE_API": len(res_api),
                    "2_NSE_RSS": len(res_rss),
                    "3_BULK_BLOCK_DEALS": len(res_deals),
                    "4_BOARD_MEETINGS_TODAY": len(res_bm),
                    "5_INSIDER_PIT": len(res_pit),
                    "6_BSE_API": len(res_bse),
                    "7_GOOGLE_NEWS": len(res_gnews),
                },
                "math_top50_matched": {k: v for k, v in math_top50_filings.items() if v},
                "broad_market_candidates_count": len(broad_market_filings),
                "broad_market_sample": dict(list(broad_market_filings.items())[:30])
            }
            with open("latest_crawled_filings.json", "w", encoding="utf-8") as df:
                json.dump(debug_dump, df, indent=4)
        except Exception as e:
            logger.warning(f"Could not write latest_crawled_filings.json: {e}")

        logger.info(
            f"Ingested filings for {sum(1 for v in math_top50_filings.values() if v)}/50 Math stocks "
            f"and {len(broad_market_filings)} Broad Market outlier candidates."
        )
        return math_top50_filings, broad_market_filings