"""Anonymous landing leads stay visible to HR; these tests send no Telegram messages."""
from sqlalchemy import text
from app.hr import queries
from tests.hr_support import run_db, add_lead


def test_anonymous_lead_is_visible_in_hr_list_export_stats_and_sources():
    async def check(sm):
        async with sm() as session, session.begin():
            lead_id=await add_lead(session,None,2,'Тестовый Кандидат','',vacancy='lab',practice_type='job')
            await session.execute(text("UPDATE leads SET source='land_test' WHERE id=:id"),{'id':lead_id})
        async with sm() as session:
            rows,_=await queries.list_leads(session,queries.LeadFilter())
            lead=next(r for r in rows if r['id']==lead_id)
            assert lead['source']=='land_test' and lead['tg_id'] is None
            assert any(r['id']==lead_id for r in await queries.export_rows(session,queries.LeadFilter()))
            sources=await queries.sources(session)
            assert next(r for r in sources if r['src']=='land_test')['leads']==1
            assert (await queries.stats(session,10))['leads']['new']>=1
    run_db(check)
