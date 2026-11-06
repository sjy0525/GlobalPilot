from datetime import datetime
from sqlalchemy import String,DateTime,Boolean,true
from sqlalchemy.orm import mapped_column,Mapped
from uuid import uuid4,UUID 
from app.db.base import Base

class User(Base):
    __tablename__="users"

    id:Mapped[UUID]=mapped_column(primary_key=True,default=uuid4)

    email:Mapped[str]=mapped_column(String(255),unique=True,nullable=False)

    password_hash:Mapped[str|None]=mapped_column(String(255),nullable=True)

    google_sub:Mapped[str|None]=mapped_column(String(255),unique=True,nullable=True)

    nickname:Mapped[str|None]=mapped_column(String(50),nullable=True)
    
    is_active:Mapped[bool]=mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )

    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.now)

    updated_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.now)
    
