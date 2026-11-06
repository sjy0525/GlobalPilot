from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.modules.users.schemas import UserCreate, UserResponse
from app.modules.users.service import register_user

router = APIRouter()

@router.post("/register", response_model=UserResponse, status_code=201)
def register(user: UserCreate, db: Session = Depends(get_db)):
    return register_user(db, user.email, user.password)
