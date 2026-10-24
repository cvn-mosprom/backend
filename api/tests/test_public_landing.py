"""Public landing: actual persistence, retry safety and privacy, independent of game auth / sim."""
import asyncio
import json
from uuid import uuid4
import pytest
from app.public_landing import PublicLeadIn, validated_public
from app.errors import AppError
from tests.engage_helpers import engage, engage_factory, years_ago  # noqa: F401


def body(**over):
    return {'fullName':'Тестовый Кандидат','birthDate':years_ago(22),'phone':'8 (999) 123-45-67',
            'university':'Тестовый колледж','roleId':'lab','department':'lab','practiceType':'paid',
            'readiness':'later','source':'land_test','consent':True,'consentTextVersion':'test-v1',
            'submissionId':str(uuid4()),'receiptToken':str(uuid4()),**over}


async def test_public_lead_without_user_and_sim(engage):
    data=body()
    response=await engage.post('/api/leads/public',json=data)
    assert response.status_code==201,response.text
    assert set(response.json())=={'ok','id'}  # no personal data / deletion secret reflected
    row=await engage.db.fetchrow('SELECT * FROM leads')
    assert row['user_id'] is None and row['source']=='land_test' and row['vacancy']=='lab'
    assert row['phone']=='+79991234567' and row['tags']==[] and row['quiz_score'] is None
    assert await engage.db.fetchval('SELECT count(*) FROM users')==0
    event=await engage.db.fetchval("SELECT payload FROM outbox WHERE kind='lead_created'")
    assert json.loads(event)['lead_id']==response.json()['id']
    assert data['receiptToken'] not in event


async def test_retry_and_concurrent_retry_create_one_lead_and_outbox(engage):
    data=body()
    replies=await asyncio.gather(*[engage.post('/api/leads/public',json=data) for _ in range(3)])
    assert all(r.status_code==201 for r in replies),[r.text for r in replies]
    assert len({r.json()['id'] for r in replies})==1
    assert await engage.db.fetchval('SELECT count(*) FROM leads')==1
    assert await engage.db.fetchval('SELECT count(*) FROM outbox')==1
    changed=await engage.post('/api/leads/public',json={**data,'fullName':'Другой Кандидат'})
    assert changed.status_code==409


async def test_public_job_does_not_require_university(engage):
    r=await engage.post('/api/leads/public',json=body(practiceType='job',university=''))
    assert r.status_code==201,r.text
    assert await engage.db.fetchval('SELECT university FROM leads')==''


@pytest.mark.parametrize('changes,status',[
    ({'birthDate':years_ago(17)},403),({'birthDate':None},403),({'consent':False},403),
    ({'university':''},422),({'department':'engineering'},422),({'roleId':'unknown'},422),
    ({'phone':'123'},422),({'fullName':'Имя'},422)])
async def test_invalid_public_leads_never_saved(engage,changes,status):
    r=await engage.post('/api/leads/public',json=body(**changes))
    assert r.status_code==status,r.text
    assert await engage.db.fetchval('SELECT count(*) FROM leads')==0


async def test_public_revocation_is_secret_and_idempotent(engage):
    data=body();await engage.post('/api/leads/public',json=data)
    url=f"/api/leads/public/{data['submissionId']}"
    assert (await engage.delete(url,headers={'Authorization':'Bearer wrong'})).status_code==404
    headers={'Authorization':f"Bearer {data['receiptToken']}"}
    assert (await engage.delete(url,headers=headers)).status_code==204
    assert (await engage.delete(url,headers=headers)).status_code==204
    assert await engage.db.fetchval("SELECT count(*) FROM outbox WHERE kind='lead_revoked'")==1
    assert (await engage.post('/api/leads/public',json=data)).status_code==409


async def test_public_events_whitelist_drops_form_contents(engage):
    data={'sessionId':str(uuid4()),'source':'land_test','events':[{'name':'role_view','role':'lab','phone':'not-stored'}]}
    r=await engage.post('/api/events/public',json=data)
    assert r.status_code==200 and r.json()['accepted']==1,r.text
    props=json.loads(await engage.db.fetchval('SELECT props FROM landing_events'))
    assert props=={'role':'lab'}
    assert (await engage.post('/api/events/public',json={**data,'events':[{'name':'arbitrary'}]})).status_code==422
