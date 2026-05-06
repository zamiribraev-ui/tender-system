# TenderAI — инструкции для автоматического агента

## Задача при автоматическом запуске (RemoteTrigger / cron)

Если сессия запущена автоматически (не вручную пользователем), выполни следующие шаги и завершись:

1. **Логин**: POST `https://web-production-874cf.up.railway.app/api/login`
   - Body: `{"username":"admin","password":"admin123"}`
   - Сохрани `token` из ответа.

2. **Поиск тендеров**: POST `https://web-production-874cf.up.railway.app/api/fetch-web`
   - Header: `Authorization: Bearer {token}`

3. **Проверка результатов**: GET `/api/tenders?client_id=3` и `/api/tenders?client_id=4`
   - Header: `Authorization: Bearer {token}`
   - Найди записи где `recommendation` = "YES" или "MAYBE" и `score` >= 60.

4. **Уведомление**:
   - Если нашлись подходящие тендеры → отправь `PushNotification`:
     `"TenderAI: {N} тендеров для Algimed/Resol — {название первого, до 80 символов}"`
   - Если ничего нет → не уведомляй, тихо завершись.

## Проект

- Backend: Python HTTP server, Railway deployment
- Frontend: `/Users/izz/tender_system/frontend/index.html`
- DB: SQLite, `tenders.db`
- Clients: Algimed (lab equipment), Resol (industrial chemistry)
