"""Local, explainable classification for normalized bill rows.

The public converters historically pass keyword rules as Excel rows shaped like
``[rule_type, category, subcategory, keyword, ...]``.  This module accepts that
format as well as explicit rule dictionaries, so the old configuration keeps
working while new rules can declare their fields and matching behavior.
"""

import re
import json
import hashlib
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


TEXT_FIELDS = ("交易对方", "商品", "交易信息", "备注")


def row_fingerprint(row):
    """Hash stable transaction fields before notes are redacted or formatted."""
    date = _text(row.get("日期", "")).replace("T", " ")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            date = datetime.strptime(date, pattern).isoformat()
            break
        except ValueError:
            pass
    amount = _text(row.get("金额", "")).replace(",", "").replace("¥", "").replace("￥", "")
    try:
        amount = format(Decimal(amount).normalize(), "f")
    except InvalidOperation:
        pass
    values = [_normalize(row.get(field, "")) for field in
              ("支付渠道", "成员", "交易类型", "交易对方")]
    values.extend((_normalize(date), _normalize(_product(row)), amount))
    transaction_id = next((_text(row.get(key)) for key in ("交易单号", "交易号", "交易订单号")
                           if _text(row.get(key))), "")
    values.append(transaction_id)
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:24]


class FeedbackStore(object):
    """Explicit confirmations, scoped by platform, member and direction.

    An entire import is validated before the store changes. Invalid JSON is
    reported instead of silently overwriting past confirmations.
    """

    def __init__(self, path):
        self.path = os.fspath(path)
        self.records = []
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            self.records = []
            return
        except (OSError, ValueError) as error:
            raise ValueError("无法读取分类经验库 %s: %s" % (self.path, error))
        if not isinstance(data, list):
            raise ValueError("分类经验库格式错误，应为记录列表")
        for record in data:
            self._validate_record(record)
        self.records = data

    @staticmethod
    def _validate_record(record):
        if not isinstance(record, dict) or not _text(record.get("category")):
            raise ValueError("分类经验库含有无效分类记录")
        if record.get("scope") not in ("transaction", "merchant", "merchant_product"):
            raise ValueError("分类经验库含有无效应用范围")
        if record.get("direction") not in ("expense", "income", "transfer", "neutral"):
            raise ValueError("分类经验库含有无效交易方向")
        if not all(isinstance(record.get(key, ""), str) for key in
                   ("category", "subcategory", "platform", "member", "keyword", "product", "id")):
            raise ValueError("分类经验库字段类型错误")
        if record["scope"] == "transaction" and not re.fullmatch(r"[0-9a-f]{24}", record.get("id", "")):
            raise ValueError("分类经验库含有无效记录标识")
        if record["scope"] != "transaction" and not _text(record.get("keyword")):
            raise ValueError("分类经验库缺少商户名称")
        if record["scope"] == "merchant_product" and not _text(record.get("product")):
            raise ValueError("分类经验库缺少商品名称")

    def save(self, records=None):
        records = self.records if records is None else records
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         prefix=".feedback-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _key(record):
        context = (record["scope"], _normalize(record.get("platform")),
                   _normalize(record.get("member")), record["direction"])
        if record["scope"] == "transaction":
            return context + (record["id"],)
        merchant = (_normalize(record["keyword"]),)
        return context + merchant + ((_normalize(record["product"]),)
                                     if record["scope"] == "merchant_product" else ())

    @staticmethod
    def _make_record(row, category, subcategory, scope):
        aliases = {"仅此笔": "transaction", "同商户": "merchant", "同商户同商品": "merchant_product"}
        scope = aliases.get(scope, scope)
        if scope not in ("transaction", "merchant", "merchant_product"):
            raise ValueError("应用范围应为：仅此笔、同商户、同商户同商品")
        direction = _direction(row)
        if direction == "unknown":
            raise ValueError("交易类型必须是收入、支出、转账或不计收支")
        category = _text(category)
        if not category:
            raise ValueError("确认一级分类不能为空")
        identity = _text(row.get("记录标识")) or row_fingerprint(row)
        if scope == "transaction" and not _text(row.get("记录标识")):
            if not all(_text(row.get(key)) for key in ("日期", "金额")):
                raise ValueError("仅此笔确认需要记录标识，或完整日期和金额")
        record = {
            "id": identity, "scope": scope,
            "keyword": _text(row.get("交易对方")), "product": _product(row),
            "platform": _text(row.get("支付渠道")), "member": _text(row.get("成员")),
            "direction": direction, "category": category, "subcategory": _text(subcategory),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        FeedbackStore._validate_record(record)
        return record

    def add(self, row, category, subcategory="", scope="transaction"):
        record = self._make_record(row, category, subcategory, scope)
        self._commit([record])
        return record

    def _commit(self, additions):
        self.load()
        merged = {self._key(record): record for record in self.records}
        count = 0
        for record in additions:
            key = self._key(record)
            old = merged.get(key)
            if old is None or (old["category"], old.get("subcategory", "")) != (record["category"], record["subcategory"]):
                merged[key] = record
                count += 1
        if count:
            records = list(merged.values())
            self.save(records)
            self.records = records
        return count

    def import_confirmations(self, rows):
        """Only explicitly filled confirmation columns are learned."""
        pending = {}
        confirmed = 0
        for number, row in enumerate(rows, 2):
            category = _text(row.get("确认一级分类"))
            if not category:
                continue
            try:
                record = self._make_record(row, category, row.get("确认二级分类", ""),
                                           _text(row.get("应用范围")) or "transaction")
                key = self._key(record)
                previous = pending.get(key)
                if previous and (previous["category"], previous["subcategory"]) != (record["category"], record["subcategory"]):
                    raise ValueError("本次文件对同一应用范围指定了不同分类，请改用仅此笔或同商户同商品")
                pending[key] = record
                confirmed += 1
            except ValueError as error:
                raise ValueError("第 %d 行: %s" % (number, error))
        changed = self._commit(list(pending.values())) if pending else 0
        return {"confirmed": confirmed, "changed": changed, "total": len(self.records)}

    def match(self, row):
        context = (_normalize(row.get("支付渠道")), _normalize(row.get("成员")), _direction(row))
        identity = _text(row.get("记录标识")) or row_fingerprint(row)
        matches = []
        for record in self.records:
            if context != (_normalize(record.get("platform")), _normalize(record.get("member")), record["direction"]):
                continue
            scope = record["scope"]
            if scope == "transaction":
                found = record["id"] == identity
            else:
                found = _normalize(record["keyword"]) == _normalize(row.get("交易对方"))
                if scope == "merchant_product":
                    found = found and _normalize(record["product"]) == _normalize(_product(row))
            if found:
                matches.append(record)
        if matches:
            return max(matches, key=lambda r: {"transaction": 3, "merchant_product": 2, "merchant": 1}[r["scope"]])
        return None


def _text(value):
    if value is None:
        return ""
    try:
        # pandas.NaN and numpy.NaN both stringify to 'nan'.
        if value != value:
            return ""
    except Exception:
        pass
    return str(value).strip()


def _normalize(value):
    return re.sub(r"[\s\u3000]+", "", _text(value)).casefold()


def _product(row):
    return _text(row.get("商品")) or _text(row.get("交易信息"))


def _direction(row):
    value = _normalize(row.get("交易类型", ""))
    return {"收入": "income", "支出": "expense", "转账": "transfer",
            "不计收支": "neutral", "中性交易": "neutral", "/": "neutral"}.get(value, "unknown")


class ClassificationRule(object):
    def __init__(self, category, subcategory="", keywords=None, fields=None,
                 priority=0, direction="expense", match="contains", excludes=None,
                 source="user"):
        self.category = _text(category)
        self.subcategory = _text(subcategory)
        self.keywords = [_text(item) for item in (keywords or []) if _text(item)]
        self.fields = list(fields or TEXT_FIELDS)
        self.priority = int(priority or 0)
        self.direction = _text(direction or "any").lower()
        self.match = _text(match or "contains").lower()
        self.excludes = [_text(item) for item in (excludes or []) if _text(item)]
        self.source = source


class ClassificationResult(object):
    def __init__(self, category="", subcategory="", confidence=0.0,
                 status="unclassified", reason="", candidates=None):
        self.category = category
        self.subcategory = subcategory
        self.confidence = confidence
        self.status = status
        self.reason = reason
        self.candidates = candidates or []


class ClassificationEngine(object):
    """Deterministic rule engine which abstains on weak or ambiguous matches."""

    def __init__(self, rules=None, threshold=0.75):
        self.rules = list(rules or [])
        self.threshold = threshold

    @classmethod
    def from_legacy_rules(cls, *rule_groups):
        rules = []
        for group in rule_groups:
            for row in group or []:
                if isinstance(row, dict):
                    rules.append(ClassificationRule(
                        category=row.get("category", row.get("一级分类", "")),
                        subcategory=row.get("subcategory", row.get("二级分类", "")),
                        keywords=row.get("keywords", row.get("关键字", [])),
                        fields=row.get("fields", row.get("匹配字段", None)),
                        priority=row.get("priority", row.get("优先级", 0)),
                        direction=row.get("direction", row.get("交易方向", "any")),
                        match=row.get("match", row.get("匹配方式", "contains")),
                        excludes=row.get("excludes", row.get("排除词", [])),
                        source=row.get("source", "user"),
                    ))
                    continue
                if not row or len(row) < 4:
                    continue
                rule_type = _text(row[0])
                direction = "income" if "收入" in rule_type else ("transfer" if "转账" in rule_type else "expense")
                category = _text(row[1]) if len(row) > 1 else ""
                subcategory = _text(row[2]) if len(row) > 2 else ""
                keywords = [_text(item) for item in row[3:] if _text(item)]
                if category and keywords:
                    rules.append(ClassificationRule(category, subcategory, keywords,
                                                    direction=direction, source="legacy"))
        return cls(rules)

    @staticmethod
    def _direction(row):
        return _direction(row)

    def classify(self, row, suggested_category="", suggested_subcategory=""):
        direction = self._direction(row)
        texts = dict((name, _normalize(row.get(name, ""))) for name in TEXT_FIELDS)
        matches = []
        for rule in self.rules:
            if rule.direction not in ("any", direction):
                continue
            if any(_normalize(exclude) in " ".join(texts.values()) for exclude in rule.excludes):
                continue
            for keyword in rule.keywords:
                needle = _normalize(keyword)
                if not needle:
                    continue
                fields = [field for field in rule.fields if field in texts]
                matching_fields = []
                exact = False
                for field in fields:
                    value = texts[field]
                    if rule.match == "exact":
                        found = value == needle
                        exact = exact or found
                    elif rule.match == "prefix":
                        found = value.startswith(needle)
                    else:
                        found = needle in value
                    if found:
                        matching_fields.append(field)
                if not matching_fields:
                    continue
                # Explicit exact merchant rules are trusted; broad legacy words
                # remain review-only rather than silently deciding a category.
                if rule.match == "exact":
                    confidence = 0.97
                elif rule.source == "legacy":
                    confidence = 0.68 if len(needle) <= 2 else min(0.84, 0.72 + 0.03 * (len(needle) - 2))
                else:
                    confidence = 0.62 if len(needle) <= 2 else min(0.91, 0.72 + 0.025 * (len(needle) - 2))
                if rule.source == "learned":
                    confidence = 0.99 if exact else max(confidence, 0.94)
                field_rank = 4 if "交易对方" in matching_fields else 3 if "商品" in matching_fields else 2
                rank = (rule.priority, 1 if rule.match == "exact" else 0, len(needle), field_rank)
                matches.append((rank, rule, keyword, matching_fields, confidence))

        if matches:
            best_rank = max(item[0] for item in matches)
            best = [item for item in matches if item[0] == best_rank]
            targets = set((item[1].category, item[1].subcategory) for item in best)
            if len(targets) > 1:
                candidates = ["%s/%s (%s)" % (r.category, r.subcategory, kw) for _, r, kw, _, _ in best]
                return ClassificationResult(status="conflict", reason="同优先级规则命中不同分类",
                                            candidates=candidates)
            _, rule, keyword, fields, confidence = best[0]
            if confidence >= self.threshold:
                return ClassificationResult(rule.category, rule.subcategory, confidence,
                                            "auto", "关键词“%s”命中%s" % (keyword, ",".join(fields)))
            candidates = ["%s/%s (%s)" % (rule.category, rule.subcategory, keyword)]
            return ClassificationResult(status="review", confidence=confidence,
                                        reason="命中宽泛关键词，建议确认", candidates=candidates)

        suggested_category = _text(suggested_category)
        suggested_subcategory = _text(suggested_subcategory)
        if suggested_category:
            return ClassificationResult(suggested_category, suggested_subcategory, 0.55,
                                        "suggested", "沿用账单自带分类，尚未由规则确认")
        return ClassificationResult(status="unclassified", reason="没有匹配到分类规则")


def apply_classification(df, expense_rules=None, income_rules=None, transfer_rules=None,
                         first_classify_col="一级分类名称", second_classify_col="二级分类名称",
                         feedback_store=None):
    """Return a copy with category decision and audit columns added."""
    engine = ClassificationEngine.from_legacy_rules(expense_rules, income_rules, transfer_rules)
    result_df = df.copy()
    # Existing platform category columns are often float dtype because their
    # values are blank; make them object before writing string decisions.
    for column in (first_classify_col, second_classify_col):
        if column in result_df.columns:
            result_df[column] = result_df[column].astype(object)
        else:
            result_df[column] = ""
    statuses, confidences, reasons, candidate_texts, identities = [], [], [], [], []
    for position, (_, row) in enumerate(result_df.iterrows()):
        raw_row = row.to_dict()
        identities.append(_text(raw_row.get("记录标识")) or row_fingerprint(raw_row))
        feedback = feedback_store.match(raw_row) if feedback_store is not None else None
        if feedback:
            scope_label = {"transaction": "仅此笔", "merchant": "同商户", "merchant_product": "同商户同商品"}[feedback["scope"]]
            decision = ClassificationResult(feedback["category"], feedback.get("subcategory", ""), 1.0,
                                            "learned", "用户确认的分类经验（%s）" % scope_label)
        else:
            decision = engine.classify(raw_row, row.get(first_classify_col, ""),
                                       row.get(second_classify_col, ""))
        if decision.status in ("auto", "learned"):
            result_df.iat[position, result_df.columns.get_loc(first_classify_col)] = decision.category
            result_df.iat[position, result_df.columns.get_loc(second_classify_col)] = decision.subcategory
        statuses.append(decision.status)
        confidences.append(decision.confidence)
        reasons.append(decision.reason)
        candidate_texts.append("; ".join(decision.candidates))
    result_df["分类状态"] = statuses
    result_df["分类置信度"] = confidences
    result_df["分类理由"] = reasons
    result_df["分类候选"] = candidate_texts
    result_df["记录标识"] = identities
    return result_df


def classify_standard_bill(df, info_data):
    """Classify the app-neutral bill format and retain a review audit trail."""
    result = apply_classification(
        df,
        expense_rules=getattr(info_data, "classify_csv_rule", []),
        income_rules=getattr(info_data, "income_classify_csv_rule", []),
        transfer_rules=getattr(info_data, "transfer_classify_csv_rule", []),
        feedback_store=getattr(info_data, "feedback_store", None),
    )
    return result
