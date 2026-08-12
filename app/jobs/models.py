"""
jobs/models.py — Data shapes for a review job.

WHY TWO SEPARATE THINGS (enum + Pydantic model)?
-------------------------------------------------
JobStatus is an enum because a job's status is a fixed set of known values.
Using an enum (rather than bare strings) means Python will yell at you at
import time if you typo "compelte" instead of "complete".

JobRecord is a Pydantic model — it's the full record we keep in the job store
for one review job. Think of it like a row in a database table.

WHY `str, Enum`?
----------------
class JobStatus(str, Enum) means the enum *is* a string. So JobStatus.PENDING
has value "pending" and IS "pending" — you can serialize it to JSON directly
without a custom encoder. If you just did `class JobStatus(Enum)`, FastAPI
wouldn't know how to turn it into a JSON string automatically.
"""
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class JobRecord(BaseModel):
    """
    The internal representation of one review job.

    This is NOT what we return to the user — that's in api/schemas.py.
    This is what we store in the job store.

    WHY default_factory FOR MUTABLE DEFAULTS?
    ------------------------------------------
    If you wrote  job_id: str = str(uuid.uuid4())  the SAME uuid would be
    used for every job (it's evaluated once at class definition time).
    default_factory=lambda: str(uuid.uuid4()) means "call this function
    each time a new JobRecord is created" — so every job gets a fresh uuid.
    Same reasoning for datetime.utcnow and the list/dict defaults.
    """

    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: JobStatus = JobStatus.PENDING
    repo_name: str

    # Absolute path to the extracted repo on disk.
    # Set when the zip is extracted; deleted when the job finishes.
    repo_path: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Which agents have finished vs. are still waiting
    completed_agents: list[str] = Field(default_factory=list)
    pending_agents: list[str] = Field(default_factory=list)

    # Raw outputs from each agent node — populated as nodes finish
    agent_results: dict[str, Any] = Field(default_factory=dict)

    # Set when status == COMPLETE
    report: Optional[dict] = None

    # Set when status == FAILED
    error: Optional[str] = None
