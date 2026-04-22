"""blog-kpop 자동화 설정."""
from pathlib import Path

# ── 경로 ──
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── 데이터 파일 ──
PROCESSED_PATH = DATA_DIR / "processed.json"    # 수집 완료된 GUID 목록
PENDING_PATH = DATA_DIR / "pending.json"         # 1단계 통과, 평가 대기
CANDIDATES_PATH = DATA_DIR / "candidates.json"   # Evaluator 통과, Writer 대기
REVIEW_ACTIONS_PATH = DATA_DIR / "review-actions.json"  # Reviewer FIX 지시

# ── RSS 피드 소스 ──
RSS_FEEDS = {
    "연합뉴스_엔터": {
        "url": "https://www.yna.co.kr/rss/entertainment.xml",
        "category": "K-pop",
        "priority": 1,
    },
    "스포츠동아": {
        "url": "https://sports.donga.com/rss",
        "category": "K-pop",
        "priority": 2,
    },
}

# ── K-pop 관련성 키워드 (1개 이상 매치되면 통과) ──
KPOP_KEYWORDS = [
    # 장르/포맷
    "아이돌", "걸그룹", "보이그룹", "솔로가수", "K팝", "K-팝", "케이팝", "케이-팝",
    # 음원/활동
    "음원", "앨범", "미니앨범", "싱글", "타이틀곡", "뮤직비디오",
    # 활동/이벤트
    "콘서트", "투어", "쇼케이스", "팬미팅", "팬사인회", "컴백", "데뷔",
    "빌보드", "멜론", "음방", "음악방송",
    # 소속사
    "하이브", "SM엔터", "JYP엔터", "YG엔터", "카카오엔터",
]

# ── K-pop 아티스트 엔티티 (2026-04-17 explore_kpop.py 실측 기반) ──
# 짧은 이름(V, 진, RM, 뷔, 지수, 지민 등)은 false positive 다발해 제외
KPOP_ENTITIES = [
    # 대형 그룹
    "BTS", "방탄소년단", "BLACKPINK", "블랙핑크", "TWICE", "트와이스",
    "NewJeans", "뉴진스", "IVE", "아이브", "aespa", "에스파",
    "LE SSERAFIM", "르세라핌", "ITZY", "있지", "Red Velvet", "레드벨벳",
    "(G)I-DLE", "(여자)아이들", "여자아이들",
    "SEVENTEEN", "세븐틴", "Stray Kids", "스트레이키즈",
    "ENHYPEN", "엔하이픈", "TOMORROW X TOGETHER", "투모로우바이투게더", "TXT",
    "ATEEZ", "에이티즈", "TREASURE", "트레저",
    "NCT", "엔시티", "WayV", "웨이션브이", "ZEROBASEONE", "제로베이스원", "ZB1",
    "BOYNEXTDOOR", "보이넥스트도어",
    "BABYMONSTER", "베이비몬스터",
    "ILLIT", "아일릿", "KISS OF LIFE", "키스오브라이프",
    "PLAVE", "플레이브",
    # 솔로
    "IU", "아이유", "BIBI", "비비", "Jennie", "제니", "Lisa", "리사", "Rosé", "로제",
    # 선발/사업 주체
    "하이브", "SM Entertainment", "JYP Entertainment", "YG Entertainment",
]

# ── 중복 체크 파라미터 ──
DEDUP_DAYS = 3  # 최근 N일 내 제목과 비교
TITLE_SIMILARITY_THRESHOLD = 0.35  # Jaccard 임계 (2026-04-17 실측)
MIN_COMMON_BIGRAMS = 6  # bigram 공통 개수 기준 (참고용)

# ── Pending 만료 ──
PENDING_MAX_AGE_DAYS = 7

# ── Evaluator ──
EVAL_BATCH_SIZE = 30  # 1회 처리 최대 건수
SCORE_THRESHOLD = 70

# ── Writer ──
WRITER_TARGET_WORD_COUNT_MIN = 500
WRITER_TARGET_WORD_COUNT_MAX = 900
