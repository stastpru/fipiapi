# main.py - FastAPI прокси-сервер для Открытого банка заданий ФИПИ (ОГЭ Физика)
import httpx
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
import logging
from bs4 import BeautifulSoup
import re
from datetime import datetime, timedelta
import ssl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Config:
    BASE_URL = "https://oge.fipi.ru"
    PROJECT_ID = "B24AFED7DE6AB5BC461219556CCA4F9B"
    DEFAULT_PAGESIZE = 10
    TIMEOUT = 30.0

config = Config()

app = FastAPI(
    title="ФИПИ Open Bank API Proxy",
    description="Прокси-сервер для Открытого банка тестовых заданий ФИПИ (ОГЭ Физика)",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnswerCheck(BaseModel):
    guid: str = Field(..., description="GUID задания")
    answer: Optional[str] = Field(None, description="Ответ (для простых типов)")
    answer_data: Optional[Dict[str, str]] = Field(None, description="Ответ (для сложных типов)")

class AnswerResult(BaseModel):
    success: bool
    correct: Optional[bool] = None
    score: Optional[int] = None
    status: int
    message: str
    correct_answer: Optional[str] = None

class QuestionBrief(BaseModel):
    id: str
    guid: str
    number: str
    kes: str
    answer_type: str
    text_preview: str
    has_image: bool
    status: int
    is_favorite: bool

class QuestionDetail(BaseModel):
    id: str
    guid: str
    number: str
    kes: str
    answer_type: str
    text_html: str
    images: List[str]
    variants: Optional[List[Dict]] = None
    hint: Optional[str] = None

class SimpleCache:
    def __init__(self, ttl_seconds=300):
        self.ttl = ttl_seconds
        self.data = {}
    def get(self, key):
        if key in self.data:
            value, timestamp = self.data[key]
            if datetime.now() - timestamp < timedelta(seconds=self.ttl):
                return value
            del self.data[key]
        return None
    def set(self, key, value):
        self.data[key] = (value, datetime.now())

cache = SimpleCache(ttl_seconds=300)

async def fetch_fipi(url: str, method: str = "GET", data: Dict = None) -> str:
    async with httpx.AsyncClient(verify=False, timeout=config.TIMEOUT, follow_redirects=True) as client:
        try:
            if method.upper() == "GET":
                response = await client.get(url)
            else:
                response = await client.post(url, data=data)
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error: {e}")
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            logger.error(f"Request error: {e}")
            raise HTTPException(status_code=500, detail=f"Ошибка связи с ФИПИ: {str(e)}")

def parse_questions_html(html: str) -> List[QuestionBrief]:
    soup = BeautifulSoup(html, 'html.parser')
    questions = []
    for qblock in soup.find_all('div', class_='qblock'):
        q_id = qblock.get('id', '')
        if q_id.startswith('q'):
            q_id = q_id[1:]
        header = qblock.find_next('div', id=f'i{q_id}')
        if not header:
            continue
        guid_input = qblock.find('input', {'name': 'guid'})
        guid = guid_input.get('value', '') if guid_input else ''
        number_span = header.find('span', class_='canselect')
        number = number_span.text if number_span else q_id
        kes_div = header.find('div', class_='param-row')
        kes = kes_div.text.strip() if kes_div else ''
        answer_type_divs = header.find_all('div', class_='param-row')
        answer_type = answer_type_divs[1].text.strip() if len(answer_type_divs) > 1 else ''
        question_text = qblock.get_text(strip=True)[:200]
        has_image = bool(qblock.find('img'))
        status_span = header.find('span', class_=re.compile(r'task-status-\d'))
        status = 0
        if status_span:
            class_str = str(status_span.get('class', []))
            status_match = re.search(r'task-status-(\d)', class_str)
            if status_match:
                status = int(status_match.group(1))
        questions.append(QuestionBrief(
            id=q_id, guid=guid, number=number, kes=kes, answer_type=answer_type,
            text_preview=question_text, has_image=has_image, status=status, is_favorite=False
        ))
    return questions

@app.get("/")
async def root():
    return {
        "service": "ФИПИ Open Bank API Proxy",
        "version": "1.0.0",
        "project": config.PROJECT_ID,
        "docs": "/docs",
        "endpoints": ["/questions", "/questions/{id}", "/check", "/themes"]
    }

@app.get("/questions")
async def get_questions(
    page: int = Query(0, ge=0, description="Номер страницы (0-based)"),
    pagesize: int = Query(10, ge=1, le=50, description="Заданий на странице"),
    theme: Optional[str] = Query(None, description="Код темы КЭС"),
    qkind: Optional[str] = Query(None, description="Тип задания"),
    qid: Optional[str] = Query(None, description="Номер задания"),
    zid: Optional[str] = Query(None, description="Номер группы"),
    solved: Optional[str] = Query("", description="0-нереш,1-реш"),
    favorite: Optional[str] = Query("", description="1-только избранные")
):
    data = {'search': 1, 'pagesize': pagesize, 'proj': config.PROJECT_ID, 'page': page}
    if theme: data['theme'] = theme
    if qkind: data['qkind'] = qkind
    if qid: data['qid'] = qid
    if zid: data['zid'] = zid
    if solved: data['solved'] = solved
    if favorite: data['favorite'] = favorite
    cache_key = f"questions_{hash(str(sorted(data.items())))}"
    cached = cache.get(cache_key)
    if cached:
        questions = cached
    else:
        url = f"{config.BASE_URL}/bank/questions.php"
        html = await fetch_fipi(url, "POST", data)
        questions = parse_questions_html(html)
        cache.set(cache_key, questions)
    return {"success": True, "page": page, "pagesize": pagesize, "total": len(questions), "questions": [q.dict() for q in questions]}

@app.get("/questions/{question_id}", response_model=QuestionDetail)
async def get_question_detail(question_id: str):
    cache_key = f"question_detail_{question_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    questions_data = await get_questions(qid=question_id)
    if not questions_data['questions']:
        raise HTTPException(status_code=404, detail=f"Задание {question_id} не найдено")
    question = questions_data['questions'][0]
    guid = question.guid
    url = f"{config.BASE_URL}/bank/questions.php"
    data = {'search': 1, 'proj': config.PROJECT_ID, 'qid': question_id}
    html = await fetch_fipi(url, "POST", data)
    soup = BeautifulSoup(html, 'html.parser')
    qblock = soup.find('div', id=f'q{question_id}')
    if not qblock:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    question_text = str(qblock.find('td', bgcolor="#FAFBCA") or qblock)
    images = []
    for img in qblock.find_all('img'):
        src = img.get('src', '')
        if src.startswith('docs/'):
            images.append(f"{config.BASE_URL}/bank/{src}")
    variants = []
    variants_table = qblock.find('table', class_='distractors-table')
    if variants_table:
        for row in variants_table.find_all('tr'):
            radio = row.find('input', {'type': 'radio'})
            if radio:
                text = row.find('td', align='left')
                variants.append({'value': radio.get('value'), 'text': text.text.strip() if text else ''})
    hint_div = qblock.find('div', id='hint')
    hint = hint_div.text.strip() if hint_div else None
    detail = QuestionDetail(
        id=question_id, guid=guid, number=question.number, kes=question.kes, answer_type=question.answer_type,
        text_html=question_text, images=images, variants=variants if variants else None, hint=hint
    )
    cache.set(cache_key, detail)
    return detail

@app.post("/check", response_model=AnswerResult)
async def check_answer(answer: AnswerCheck):
    url = f"{config.BASE_URL}/bank/solve.php"
    data = {'guid': answer.guid, 'ajax': '1', 'proj': config.PROJECT_ID}
    if answer.answer_data:
        data.update(answer.answer_data)
        answer_parts = []
        for key in sorted(data.keys()):
            if key.startswith('ans') and data[key]:
                answer_parts.append(str(data[key]))
        data['answer'] = ''.join(answer_parts)
        logger.info(f"Complex answer for {answer.guid}: {data['answer']}")
    elif answer.answer:
        data['answer'] = answer.answer
        logger.info(f"Simple answer for {answer.guid}: {answer.answer}")
    else:
        return AnswerResult(success=False, correct=False, score=0, status=0, message="Ошибка: не указан ни answer, ни answer_data", correct_answer=None)
    try:
        async with httpx.AsyncClient(timeout=config.TIMEOUT, follow_redirects=True) as client:
            response = await client.post(url, data=data)
            response.raise_for_status()
            response_text = response.text.strip()
            logger.info(f"FIPI response for {answer.guid}: '{response_text}'")
        status_map = {'0': 0, '1': 1, '2': 2, '3': 3}
        status = status_map.get(response_text, 0)
        is_correct = (status == 3)
        score = 2 if status == 3 else (1 if status == 1 else 0)
        message = "Ответ верный!" if status == 3 else ("Ответ неверный" if status == 2 else ("Ответ принят (требуется проверка)" if status == 1 else "Ошибка при проверке ответа"))
        return AnswerResult(success=(status != 0), correct=is_correct if status in [2,3] else None, score=score, status=status, message=message, correct_answer=None)
    except httpx.TimeoutException:
        return AnswerResult(success=False, correct=False, score=0, status=0, message="Превышено время ожидания", correct_answer=None)
    except Exception as e:
        return AnswerResult(success=False, correct=False, score=0, status=0, message=f"Ошибка: {str(e)}", correct_answer=None)

@app.get("/themes")
async def get_themes():
    cache_key = "all_themes"
    cached = cache.get(cache_key)
    if cached:
        return cached
    url = f"{config.BASE_URL}/bank/index.php?crproj={config.PROJECT_ID}"
    html = await fetch_fipi(url)
    soup = BeautifulSoup(html, 'html.parser')
    themes = []
    dropdown = soup.find('div', class_='dropdown')
    if dropdown:
        items = dropdown.find_all('li', class_='dropdown-item')
        current_parent = None
        for item in items:
            if 'dropdown-header' in item.get('class', []):
                current_parent = item.text.strip()
                themes.append({"code": current_parent.split()[0] if current_parent else None, "name": current_parent, "parent": None, "type": "section"})
            else:
                checkbox = item.find('input', {'type': 'checkbox'})
                if checkbox:
                    themes.append({"code": checkbox.get('value', ''), "name": item.text.strip(), "parent": current_parent, "type": "topic"})
    cache.set(cache_key, themes)
    return themes

@app.post("/favorites/{guid}")
async def add_favorite(guid: str):
    return {"success": True, "message": f"Задание {guid} добавлено в избранное", "guid": guid}

@app.delete("/favorites/{guid}")
async def remove_favorite(guid: str):
    return {"success": True, "message": f"Задание {guid} удалено из избранного", "guid": guid}

@app.get("/status/{guid}")
async def get_question_status(guid: str):
    return {"success": True, "guid": guid, "status": 0, "is_favorite": False}

@app.get("/export/questions.json")
async def export_questions_json(theme: Optional[str] = Query(None), limit: int = Query(50, ge=1, le=500)):
    questions_data = await get_questions(theme=theme, pagesize=limit)
    return JSONResponse(content=questions_data['questions'])

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"{request.method} {request.url.path}")
    response = await call_next(request)
    logger.info(f"Response: {response.status_code}")
    return response

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True, log_level="info")