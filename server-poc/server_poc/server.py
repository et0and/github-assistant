from fastapi import FastAPI, HTTPException, BackgroundTasks
from .models import GithubRepoInfo, PipelineStatus, UserPrompt, PromptType
from sqlmodel import Session, create_engine, select
from sqlalchemy import Engine
from relta import Client
from relta.datasource import DataSource
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import os.path
from typing import Optional
from datetime import datetime, timedelta
import traceback

    
load_dotenv()

TEMPLATE_SOURCE_NAME = "yonom_assistantui"

app = FastAPI()

class Prompt(BaseModel):
    # chat_id: str
    prompt: str


class Feedback(BaseModel):
    type: str
    message: dict


# Move global variables into a class for better organization
class ServerState:
    def __init__(self):
        self.client: Optional[Client] = None
        self.database_uri: Optional[str] = None
        self.engine: Optional[Engine] = None

server_state = ServerState()



def _get_repo_source_name(owner: str, repo_name: str) -> str:
    with Session(server_state.engine) as session:
        repo_info = session.exec(
             select(GithubRepoInfo)
                .where(GithubRepoInfo.owner == owner)
                .where(GithubRepoInfo.repo == repo_name)
            ).first()        

    
        if repo_info is None:
            raise HTTPException(
                status_code=404,
                detail=f"Repository '{repo_name}' data is not connected"
            )
    
        source_name = repo_info.source_name()
    
    return source_name


def _create_relta_source_and_deploy_semantic_layer(owner: str, repo_name: str) -> DataSource:
    template_source = server_state.client.get_datasource(TEMPLATE_SOURCE_NAME)
    with Session(server_state.engine) as session:
        # Get a fresh copy of the repo object within this session
        repo = session.exec(
            select(GithubRepoInfo)
            .where(GithubRepoInfo.owner== owner)
            .where(GithubRepoInfo.repo==repo_name)
        ).first()
    
        source = server_state.client.get_or_create_datasource(
            connection_uri=f"{server_state.database_uri}/{repo.source_name()}",
            name=repo.source_name()
        )

        source.semantic_layer.copy(template_source)
        source.deploy()

        return source



def _format_data(sql: str, data: list[tuple]) -> list[dict]:
    """Convert database tuple results into a list of dicts with column names as keys.
    Also converts datetime objects to ISO format strings.
    
    Args:
        sql: The SQL query that was executed (used to extract column names)
        data: List of tuples containing the query results
        
    Returns:
        List of dictionaries with column names as keys
    """
    from datetime import datetime
    from sqlglot import parse_one, exp

    # Extract column names from SQL query using sqlglot
    columns = []
    try:
        parsed = parse_one(sql)
        for select in parsed.find_all(exp.Select):
            for projection in select.expressions:
                columns.append(projection.alias_or_name)
    except Exception as e:
        print(e)
        return []

    if not columns:
        return []
    
    # Convert each tuple to a dict with column names
    formatted_data = []
    for row in data:
        row_dict = {}
        for i, value in enumerate(row):
            # Convert datetime objects to ISO format strings
            if isinstance(value, datetime):
                value = value.isoformat()
            row_dict[columns[i]] = value
        formatted_data.append(row_dict)
    
    return formatted_data


def initialize_server(force_refresh: bool = False):
    """Initialize the server with GitHub data source"""
    server_state.database_uri = os.environ.get('GITHUB_DATABASE_CONNECTION_URI')

    # Setup CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Initialize database
    server_state.engine = create_engine(f'{server_state.database_uri}/github_assistant')
    # Create all tables using the GitHub-specific metadata
    GithubRepoInfo.metadata.create_all(server_state.engine)
    UserPrompt.metadata.create_all(server_state.engine)

    # Initialize Relta client
    server_state.client = Client()





@app.post("/data")
def add_prompt_get_data(prompt: Prompt, owner: str, repo_name: str, background_task: BackgroundTasks):
    # Check repo name validity before entering try block
    try:
        source = _create_relta_source_and_deploy_semantic_layer(owner, repo_name)       
        chat = server_state.client.create_chat(source)
        background_task.add_task(record_user_prompt, prompt.prompt, owner, repo_name, PromptType.FULL_TEXT)
        response = chat.prompt(prompt.prompt, mode='data_only')
        if response.sql is not None:
            response.sql_result = _format_data(sql=response.sql, data = response.sql_result)
        return response
        
    except Exception as e:
        print(traceback.format_exc())
        print(e)
        raise HTTPException(
            status_code=500,
            detail="An unknown error occured"
        )


@app.post("/prompt")
def add_prompt_to_chat(prompt: Prompt, owner:str,  repo_name: str, background_task: BackgroundTasks):
    # Check repo_name validity before entering try block
    try:        
        source = _create_relta_source_and_deploy_semantic_layer(owner, repo_name)       
        background_task.add_task(record_user_prompt, prompt.prompt, owner, repo_name, PromptType.FULL_TEXT)
        chat = server_state.client.create_chat(source)
        response = chat.prompt(prompt.prompt, debug=True)
        return response
        
    except Exception as e:
        print(e)
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail="An unknown error occured"
        )

@app.post("/feedback")
def record_feedback(feedback: Feedback):
    if feedback.type == "positive":
        print("ignoring positive feedback")
        return {"status": "success"}

    print(feedback.message)
    match_resp = None
    for resp in server_state.chat.responses:
        if resp.text == feedback.message["content"][0]["text"]:
            match_resp = resp
            break
    if match_resp:
        match_resp.feedback(feedback.type)
    else:
        print("Couldn't find matching Response for feedback")

    layer = server_state.source.semantic_layer
    layer.refine(pr=True)
    return {"status": "success"}

@app.get("/")
async def root():
    return {"message": "hello"}

def record_user_prompt(prompt: str, owner: str, repo_name: str, prompt_type: PromptType):
    with Session(server_state.engine) as session:
        prompt = UserPrompt(
            prompt = prompt,
            owner = owner, 
            repo = repo_name,
            prompt_type= prompt_type,
            time = datetime.now()
        )
        session.add(prompt)
        session.commit()




def background_pipeline_run(repo_id: int, access_token: str):
    # Create a new session for this background task
    with Session(server_state.engine) as session:
        # Get a fresh copy of the repo object within this session
        repo = session.exec(
            select(GithubRepoInfo)
            .where(GithubRepoInfo.id == repo_id)
        ).first()
        try:
            # Load the data
            repo.load_data(access_token)
            # Commit the changes
            session.add(repo)
            _create_relta_source_and_deploy_semantic_layer(repo.owner, repo.repo)       
            session.commit()
            
            
        except Exception as e:
            session.rollback()
            raise e

@app.post("/load-github-data", status_code=201)
async def load_github_data(
    owner: str,
    repo: str,
    access_token: str,
    background_tasks: BackgroundTasks,
    load_issues: bool = True,
    load_pull_requests: bool = True,
    load_stars: bool = True,
    load_commits: bool = True
  
):
    MIN_REFRESH_SECONDS = int(os.environ.get('MIN_REFRESH_SECONDS', 3600))  # Default 1 hour
    try:
        with Session(server_state.engine) as session:
            repo_info = session.exec(
                select(GithubRepoInfo)
                .where(GithubRepoInfo.owner == owner)
                .where(GithubRepoInfo.repo == repo)
            ).first()            

            if repo_info is not None:
                # Update loading preferences
                repo_info.load_issues = load_issues
                repo_info.load_pull_requests = load_pull_requests
                repo_info.load_stars = load_stars
                repo_info.load_commits = load_commits
                repo_info.access_token = access_token

                if(repo_info.pipeline_status == PipelineStatus.RUNNING):
                    return {
                        "status": "success",
                        "message": "Pipeline is currently running",

                    }
                
                # Check if enough time has passed since last successful run
                time_since_last_run = datetime.now() - (repo_info.last_pipeline_run or datetime.min)
                if (repo_info.pipeline_status != PipelineStatus.SUCCESS or 
                    time_since_last_run > timedelta(seconds=MIN_REFRESH_SECONDS)):
                    background_tasks.add_task(background_pipeline_run, repo_info.id, access_token) 
                    repo_info.pipeline_status = PipelineStatus.RUNNING
                    session.add(repo_info)
                    session.commit()
                    return {
                        "status": "success",
                        "message": "Pipeline run trigerred",
                        "last_refresh": repo_info.last_pipeline_run.isoformat(),
                    }
                else:
                    return {
                        "status": "success",
                        "message": "Data is up to date",
                        "last_refresh": repo_info.last_pipeline_run.isoformat()
                    }
            else:
                repo_info = GithubRepoInfo(
                    owner=owner,
                    repo=repo,
                    load_issues=load_issues,
                    load_pull_requests=load_pull_requests,
                    load_stars=load_stars,
                    load_commits=load_commits,
                    access_token=access_token
                )
                repo_info.setup_destination_db(server_state.database_uri)
                repo_info.pipeline_status = PipelineStatus.RUNNING
                session.add(repo_info)
                session.commit()
                background_tasks.add_task(background_pipeline_run, repo_info.id, access_token) 
                return {
                    "status": "success",
                    "message": "Pipeline run trigerred"
                }

    except Exception as e:
        print(e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load GitHub data: {str(e)}"
        )

# Initialize server when module loads
initialize_server(force_refresh=bool(os.environ.get('FORCE_REFRESH')))


