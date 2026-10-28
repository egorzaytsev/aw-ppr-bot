"""ID таблиц и полей базы EEU Ecosystem CRM (appD8N8DAgcTAqfL1).

Работаем только по ID: переименование поля в интерфейсе бота не ломает.
"""


class People:
    TABLE = "tbl4RPfMHqwknbwgm"
    NAME = "fld8on62aAuq1mN05"
    COMPANY = "fld6rHvQxGWlJpKGK"
    TELEGRAM_USERNAME = "fld7VJ6fzvST6vTL2"
    TELEGRAM_CHAT_ID = "fldkrdC4GxwySZaOb"
    BOT_STATE = "fldTgLLWJ8c7uYFlI"
    REMINDER_SLOT = "fldspBKdbk8Ed9Ji5"
    LAST_REMINDER = "fldpIUrdICo7UusyN"
    COUNTRY = "fldKd7jlAKfey5xeD"
    ROLE = "fldrG4pHBV6Fxo4Xf"
    REGISTRATIONS = "fldWLjaQBXmtoT5xX"   # ← Registrations.Submitted By
    # Обратные стороны Manager-полей Projects
    AS_NOTIFIED = "flddsYlUWQ3xAeZg7"
    AS_PRE_REGISTERED = "fld7ZUpWY67F2Cr7L"
    AS_REGISTERED = "fld7LlH74M7m7XgTb"


class Companies:
    TABLE = "tblUnWupN3QzpaWnj"
    REGISTRATIONS = "fldkMDN9qrMlcKQzi"      # ← Registrations.Dealer
    PEOPLE = "fld9kyOncMPx5R5GC"
    NAME = "flddHSCWNh1RDWy9e"
    TYPE = "fldIjlQabi2MbWHB1"
    COUNTRY = "fld18L0SpuGcNIGOD"


class Projects:
    TABLE = "tbl3ezrJ2Zssu4Aoz"
    NAME = "fldxdxuL2j3aBKGBZ"
    STATUS = "flddwm09GKIgI34Xk"
    EXW_POTENTIAL = "fldOZRM2pbEqNncM3"
    STATUS_EXPIRY = "fldVVCjFCegofCFj8"
    PPR_TRACK = "flds53e1gwOkJXqoc"
    PPR_MISSING = "fldsISktQoNsFzSxD"
    END_CLIENT = "fldQVuLzwDnPZ9xs0"
    OBJECT_NAME = "fldTqHXUNdauIkzUF"
    OBJECT_PART = "fldN8piCNAdvidzhJ"
    COUNTRY = "fld72PcE4DP8QyR7k"
    CITY = "fldhezZhV3OJXRSy1"
    OBJECT_ADDRESS = "fldFTPeQ2EbgaYw4b"
    PROJECT_STAGE = "fldgigF1Vu9dad1iW"
    SPECIFIER = "fldEqF7n7a6QZhOrs"
    AW_ALTERNATIVES = "fldTqroFvjJD0V86s"
    EXPECTED_ORDER_DATE = "fldgxrK1xFo4V2tpN"
    CONTACT_LEVEL = "fldQzxIA7iQx9M50m"
    PARALLEL_REGISTRATIONS = "fld8Nw4DWPyEDkcUs"
    DOSSIER_FILES = "fldlZAQm173wqC7hh"
    DOSSIER_LINKS = "fldlFRIVYAOdjTAF0"   # ссылки на материалы, по одной на строку
    PEOPLE = "fldLN8Lj9uqQpRzza"          # участники проекта (ГАП и др.)
    QUOTATION = "fld2eKHFMhU73fxS5"
    DEALER = "fldpdzk9bY1jFMPeJ"
    PROJECT_LOG = "fldC3PJr8sT9iZ8Rg"
    PPR_REF = "fld9H3DLTtTOTluOb"            # формула AW-YY-####
    PPR_NEXT_MISSING = "fldVIAWgivyBeKX4a"   # формула: чего не хватает для следующего этапа
    PROBABILITY = "fldHldhYLswedJe8s"
    # Статусы PPR — у менеджера дилера (ссылки на People); компания получает скидку через него.
    # Старые поля по компаниям (Notified fldiKRdTlngI6AXK8, Pre-registered fldSXoNH5rwWCYaWT,
    # Registered fld75C5ApwGwVCT5N) не читаем и не пишем
    NOTIFIED = "fldJRkBhuJzyg7QIX"        # Notified (Manager)
    PRE_REGISTERED = "fldr4KkTqqgDZm7u6"  # Pre-registered (Manager)
    REGISTERED = "fldGaXBjJetxthhqv"      # Registered (Manager)
    STATUS_CONFIRMED = "fldtmomY3vt087h7I"


class Registrations:
    TABLE = "tbl4lKbYhOkVIYNHQ"
    NAME = "fldjPMiApjXLsf4tL"
    REG_STATUS = "fldALr7pPzYZsDRYy"
    DEALER_PROJECT_NAME = "fldV9fwrEBmmyrax3"
    SUBMITTED_BY = "fldnFrZK0BZ5SMDhm"
    DEALER = "fldlqAun7KrwyiQVL"
    REQUEST_TYPE = "fldyEdT3wt8jn5O4k"
    TARGET_PROJECT = "fldJg1uc4mfJ9Gquf"
    PAYLOAD = "fldpXPIZoGwG2qlXn"
    ATTACHMENTS = "fldGXPvBaN3QNVq0p"
    SUBMITTED_AT = "fldXxfsMD29jkpqOa"
    DECISION = "fldVa9VFrZtahGCwi"
    DECISION_NOTE = "fldL7Ncl5Ess8jsqQ"
    BOT_PROCESSED = "fld46XTTXBfX30llt"
    ANALYSIS = "fldZk7APhOh9p3rsx"
    QUOTE_TERMS = "fld2FPH6Neqps34Ca"
    QUOTE_NO = "fldrzhovqmXoGiEFp"
    QUOTE_VALID = "fldn9Rvq4Y5WufPwJ"
    PROCESSED = "fldQFFIL7xmiTZ12a"      # checkbox: заявка закрыта
    BOT_REG_STATUS = "fldwvOjv1I3iwTiV5"  # Reg Status, который бот уже обработал (смена — событие)
    PROCESSED_AT = "fldQZsVVnaskL3BY9"


class RegStatus:
    PENDING = "PENDING"
    NOTIFICATION = "Notification"
    PRE_REGISTRATION = "Pre-registration"
    REGISTRATION = "Registration"
    DECLINED = "Declined"


class Buffer:
    """Буфер: всё новое ждёт разбора Егора; в основную базу — только после одобрения."""
    TABLE = "tblkAlRuU99WwYtKM"
    WHAT = "fldSbtsh9PJiP53ou"
    SOURCE = "fldwdgJaWQ3vDoj5T"      # Telegram / Письмо / Квотация / Проформа / Инвойс / Аудио
    RECEIVED = "fld4rF7k13dGLIvKL"
    PROJECT = "fldT3njOIwDoY7THg"
    ESSENCE = "fldeoObFm1krSUePf"
    SOURCE_REF = "fldmzsXCpq3w3KOhv"
    STATUS = "fldNjTs2z5MhglbSo"      # Новое / Отложено
    FILES = "fldXGqMVzFWurwhou"
    KEY = "fldKohxc59eYlQHw0"          # tg:<chat>:<message> — защита от дублей


class Decision:
    ACCEPT = "Accept"
    ACCEPT_MERGE = "Accept — merge into Target Project"
    DECLINE = "Decline"
    NEED_INFO = "Need more info"


class RequestType:
    NEW_NOTIFICATION = "New — Notification"
    NEW_PRE_REGISTRATION = "New — Pre-registration"
    # В поле Request Type этого варианта пока нет — Егор добавляет вручную
    NEW_REGISTRATION = "New — Registration"
    UPGRADE_PRE_REGISTRATION = "Upgrade — Pre-registration"
    UPGRADE_REGISTRATION = "Upgrade — Registration"
    # Старый общий тип обновления — у ранних заявок; новые пишутся как fields / news
    UPDATE_INFO = "Update — info only"
    UPDATE_FIELDS = "Update — fields"   # «Дополнить материалы»
    UPDATE_NEWS = "Update — news"       # «Сообщить новость» и ответ на запрос
    UPDATE_WON = "Update — Won"
    UPDATE_LOST = "Update — Lost"
    QUOTE_SIMPLIFIED = "Quotation — Simplified"
    UNKNOWN_USER = "Unknown user"
    INFO_UPDATES = frozenset({UPDATE_INFO, UPDATE_FIELDS, UPDATE_NEWS})
