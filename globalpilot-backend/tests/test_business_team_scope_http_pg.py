"""Full application HTTP authorization with real JWTs and normal DB dependencies.

Only the application lifespan is disabled: tests initialize the database schema
explicitly, and never initialize external model clients or background workers.
The supplied isolated test database must be disposable and initially empty.
"""

import json
import os
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_team_authorization_pg import isolated_database_url


@unittest.skipUnless(os.getenv("TEAM_AUTH_TEST_DATABASE_URL"), "requires isolated PostgreSQL")
class BusinessTeamScopeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        expected = isolated_database_url(os.environ["TEAM_AUTH_TEST_DATABASE_URL"])
        from app import main
        from app.db import database
        from app.modules.brands.models import Brand
        from app.modules.briefs.models import Brief
        from app.modules.rag.models import KnowledgeDocument
        from app.modules.teams.models import Team, TeamMember
        from app.modules.users.models import User
        from app.modules.auth.security import hash_password
        from app.modules.brands.service import seed_all_team_brands

        actual = database.engine.url.difference_update_query(["options"])
        if actual != expected:
            raise AssertionError("Normal application engine must point at the explicitly supplied test DB")
        database.engine.echo = False
        cls.main, cls.database = main, database
        cls.TeamMember, cls.User = TeamMember, User
        with database.engine.begin() as conn:
            main._prepare_schema(conn)
        cls.a, cls.b, cls.user_id, cls.other_id = [uuid4() for _ in range(4)]
        cls.email=f"http-team-{cls.user_id}@acceptance.invalid"
        cls.password = "business-team-http-test"
        with database.sessionLocal.begin() as db:
            db.add_all([
                Team(id=cls.a,name="HTTP team A"),Team(id=cls.b,name="HTTP team B"),
                User(id=cls.user_id,email=cls.email,password_hash=hash_password(cls.password),is_active=True),
                User(id=cls.other_id,email=f"http-other-{cls.other_id}@acceptance.invalid",is_active=True),
            ])
            db.flush()
            db.add_all([
                TeamMember(team_id=team,user_id=user,role="employee",status="active")
                for team in (cls.a,cls.b) for user in (cls.user_id,cls.other_id)
            ])
            seed_all_team_brands(db)
            cls.brand_a = db.scalar(select(Brand.id).where(Brand.team_id==cls.a,Brand.name=="Audio"))
            cls.brand_b = db.scalar(select(Brand.id).where(Brand.team_id==cls.b,Brand.name=="Audio"))
            cls.doc_a,cls.doc_b,cls.brief_a,cls.brief_b,cls.other_brief = [uuid4() for _ in range(5)]
            for team,brand,doc,brief in ((cls.a,cls.brand_a,cls.doc_a,cls.brief_a),(cls.b,cls.brand_b,cls.doc_b,cls.brief_b)):
                db.add(KnowledgeDocument(id=doc,team_id=team,brand_id=brand,source="http.txt",body="team-private",file_bytes=str(team).encode(),media_type="text/plain",chunk_count=0))
                db.add(Brief(id=brief,team_id=team,brand_id=brand,user_id=cls.user_id,goal="owned",selling_points="facts",region="eu-na",user_persona="buyer",social_media=["instagram"],workflow_status="completed"))
            db.add(Brief(id=cls.other_brief,team_id=cls.a,brand_id=cls.brand_a,user_id=cls.other_id,goal="other",selling_points="facts",region="eu-na",user_persona="buyer",social_media=["instagram"],workflow_status="completed"))

        @asynccontextmanager
        async def no_lifespan(app):
            yield

        cls.lifespan_patch = patch.object(main.app.router,"lifespan_context",no_lifespan)
        cls.lifespan_patch.start()
        cls.addClassCleanup(cls.lifespan_patch.stop)
        cls.client = TestClient(main.app)
        cls.client.__enter__()
        cls.addClassCleanup(cls.client.__exit__,None,None,None)
        response = cls.client.post("/api/auth/login",json={"email":cls.email,"password":cls.password})
        if response.status_code != 200:
            raise AssertionError(f"Real password login failed: {response.status_code}")
        cls.token = response.json()["access_token"]
        cls.headers={"Authorization":"Bearer "+cls.token}
        cls.checks=[]

    @classmethod
    def tearDownClass(cls):
        report=Path("test-results/team-business-authorization/full-app-http.json")
        report.parent.mkdir(parents=True,exist_ok=True)
        report.write_text(json.dumps({"dependency_overrides":False,"lifespan_disabled":True,"real_password_login":True,"real_postgresql":True,"checks":cls.checks},ensure_ascii=False,indent=2))

    def setUp(self):
        with self.database.sessionLocal.begin() as db:
            db.get(self.User,self.user_id).is_active=True
            member=db.scalar(select(self.TeamMember).where(self.TeamMember.team_id==self.a,self.TeamMember.user_id==self.user_id))
            member.status="active"
            member.role="employee"

    def call(self,method,path,expected,*,params=None,headers=None,**kwargs):
        response=self.client.request(method,path,params=params,headers=headers,**kwargs)
        self.checks.append({"method":method,"path":path,"expected":expected,"actual":response.status_code})
        self.assertEqual(response.status_code,expected,response.text[:300])
        return response

    def endpoints(self):
        unknown=str(uuid4())
        brief_body={"brand_id":str(self.brand_a),"goal":"goal","selling_points":"facts","region":"eu-na","user_persona":"buyer","social_media":["instagram"]}
        return [
            ("GET","/api/brands",{}),
            ("POST","/api/documents",{"files":{"file":("http.txt",b"text")}}),
            ("GET","/api/documents",{}),
            ("GET",f"/api/documents/{unknown}/file",{}),
            ("PUT",f"/api/documents/{unknown}",{"files":{"file":("http.txt",b"text")}}),
            ("DELETE",f"/api/documents/{unknown}",{}),
            ("POST","/api/query",{"json":{"brand_id":str(self.brand_a),"question":"question"}}),
            ("POST","/api/briefs",{"json":brief_body}),("GET","/api/briefs",{}),
            ("POST","/api/briefs/workflow",{"json":{"brief_id":unknown}}),
            ("GET","/api/briefs/workflow",{}),
            ("POST","/api/briefs/workflow/stream",{"json":{"brief_id":unknown}}),
            ("POST","/api/briefs/workflow/review",{"json":{"brief_id":unknown,"action":"approved","expected_checkpoint_id":"test"}}),
            ("POST","/api/briefs/workflow/review/stream",{"json":{"brief_id":unknown,"action":"approved","expected_checkpoint_id":"test"}}),
            ("GET",f"/api/briefs/{unknown}/memories",{}),
            ("GET","/api/evals/snapshot",{}),("GET","/api/evals/testset",{}),
            ("GET","/api/evals/runs",{}),("POST","/api/evals/runs",{"json":{"workflow_version":"current"}}),
            ("GET",f"/api/evals/runs/{unknown}",{}),("GET",f"/api/evals/runs/{unknown}/tasks",{}),
            ("POST",f"/api/evals/runs/{unknown}/baseline",{}),("POST",f"/api/evals/runs/{unknown}/report",{}),
            ("GET","/api/evals/compare",{}),("PATCH",f"/api/evals/tasks/{unknown}",{"json":{"facts":"pass","note":"test"}}),
        ]

    def matrix(self,expected,*,headers=None,team=True):
        for method,path,kwargs in self.endpoints():
            with self.subTest(method=method,path=path):
                params={"brand_id":str(self.brand_a),"expected_version":1,"brief_id":str(self.brief_a),"left":str(uuid4()),"right":str(uuid4())}
                if team:
                    params["team_id"]=str(self.a)
                self.call(method,path,expected,params=params,headers=headers,**kwargs)

    def test_all_25_endpoints_reject_anonymous_requests(self):
        self.matrix(401)

    def test_all_25_endpoints_require_explicit_team_for_logged_in_user(self):
        self.matrix(422,headers=self.headers,team=False)

    def test_all_25_endpoints_recheck_removed_membership_with_same_jwt(self):
        with self.database.sessionLocal.begin() as db:
            member=db.scalar(select(self.TeamMember).where(self.TeamMember.team_id==self.a,self.TeamMember.user_id==self.user_id))
            member.status="removed"
        self.matrix(403,headers=self.headers)

    def test_all_25_endpoints_recheck_disabled_account_with_same_jwt(self):
        with self.database.sessionLocal.begin() as db:
            db.get(self.User,self.user_id).is_active=False
        self.matrix(403,headers=self.headers)

    def test_normal_dependencies_isolate_brands_documents_and_brief_lists(self):
        params={"team_id":str(self.a),"brand_id":str(self.brand_a)}
        brands=self.call("GET","/api/brands",200,params=params,headers=self.headers).json()
        self.assertEqual(len(brands),3)
        self.assertIn(str(self.brand_a),{row["id"] for row in brands})
        self.assertNotIn(str(self.brand_b),{row["id"] for row in brands})
        docs=self.call("GET","/api/documents",200,params=params,headers=self.headers).json()
        self.assertEqual([row["id"] for row in docs],[str(self.doc_a)])
        content=self.call("GET",f"/api/documents/{self.doc_a}/file",200,params=params,headers=self.headers)
        self.assertEqual(content.content,str(self.a).encode())
        briefs=self.call("GET","/api/briefs",200,params=params,headers=self.headers).json()
        self.assertEqual([row["id"] for row in briefs],[str(self.brief_a)])

    def test_cross_team_and_other_owner_resources_are_uniformly_hidden(self):
        params={"team_id":str(self.a),"brand_id":str(self.brand_a)}
        self.call("GET","/api/documents",404,params=params|{"brand_id":str(self.brand_b)},headers=self.headers)
        self.call("GET",f"/api/documents/{self.doc_b}/file",404,params=params,headers=self.headers)
        for brief in (self.brief_b,self.other_brief):
            self.call("GET",f"/api/briefs/{brief}/memories",404,params=params,headers=self.headers)
            self.call("GET","/api/briefs/workflow",404,params=params|{"brief_id":str(brief)},headers=self.headers)

    def test_existing_openapi_urls_declare_required_team_query_parameter(self):
        schema=self.main.app.openapi()
        for method,path,_ in self.endpoints():
            # Find templates for generated resource IDs.
            candidates=[(p,item[method.lower()]) for p,item in schema["paths"].items() if method.lower() in item and
                        len(p.split("/"))==len(path.split("/")) and all(a==b or a.startswith("{") for a,b in zip(p.split("/"),path.split("/")))]
            self.assertEqual(len(candidates),1,(method,path))
            params=candidates[0][1].get("parameters",[])
            self.assertTrue(any(p["name"]=="team_id" and p["in"]=="query" and p["required"] for p in params),(method,path))
