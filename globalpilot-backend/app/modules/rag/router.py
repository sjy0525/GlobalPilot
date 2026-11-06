from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.teams.dependencies import get_current_member_from_query
from app.db.database import get_async_db
from app.modules.teams.models import TeamMember
from app.modules.brands.models import Brand
from app.modules.rag.processing.loaders import aload_document_upload, media_type_for
from app.modules.rag.service import (
    aask,
    adelete_document,
    adocument_file,
    aindex_text,
    alist_documents,
    adocument_structure,
    adocument_content,
    adocument_asset,
)
from app.modules.rag.schemas import RagQuery

router = APIRouter()


async def _get_brand_or_404(db: AsyncSession, brand_id: UUID, *, team_id: UUID) -> Brand:
    result = await db.execute(select(Brand).where(Brand.id == brand_id, Brand.team_id == team_id))
    brand = result.scalar_one_or_none()
    if brand is None:
        raise HTTPException(status_code=404, detail="品牌不存在")
    return brand


async def _read_upload(file: UploadFile) -> dict:
    filename = file.filename or "untitled"
    content = await file.read()
    parsed = await aload_document_upload(filename, content)
    return {**parsed, "file_bytes": content, "media_type": media_type_for(parsed["source"])}


@router.post("/documents", status_code=201)
async def upload_document(
    brand_id: UUID,
    request: Request,
    response: Response,
    member: TeamMember = Depends(get_current_member_from_query),
    db: AsyncSession = Depends(get_async_db),
    file: UploadFile = File(),
):
    await _get_brand_or_404(db, brand_id, team_id=member.team_id)
    # Finish the read transaction before parsing or waiting for embeddings.
    await db.rollback()
    parsed = await _read_upload(file)
    document = await aindex_text(
        db=db,
        brand_id=brand_id,
        **parsed,
        team_id=member.team_id,
        actor_user_id=member.user_id,
    )
    response.headers["Location"] = str(
        request.url_for("download_document", document_id=document["id"]).include_query_params(
            brand_id=str(brand_id), team_id=str(member.team_id)
        )
    )
    return document


@router.get("/documents")
async def get_documents(
    brand_id: UUID,
    member: TeamMember = Depends(get_current_member_from_query),
    db: AsyncSession = Depends(get_async_db),
):
    brand = await _get_brand_or_404(db, brand_id, team_id=member.team_id)
    return await alist_documents(db, brand.id, team_id=member.team_id, actor_user_id=member.user_id)


@router.get("/documents/{document_id}/file")
async def download_document(
    document_id: UUID,
    brand_id: UUID,
    expected_version: int | None = Query(None, ge=1),
    member: TeamMember = Depends(get_current_member_from_query),
    db: AsyncSession = Depends(get_async_db),
):
    brand = await _get_brand_or_404(db, brand_id, team_id=member.team_id)
    return await adocument_file(db, brand.id, document_id, team_id=member.team_id, actor_user_id=member.user_id,
                                expected_version=expected_version)


@router.put("/documents/{document_id}")
async def update_document(
    document_id: UUID,
    brand_id: UUID,
    expected_version: int = Query(..., ge=1),
    member: TeamMember = Depends(get_current_member_from_query),
    db: AsyncSession = Depends(get_async_db),
    file: UploadFile = File(),
):
    await _get_brand_or_404(db, brand_id, team_id=member.team_id)
    await db.rollback()
    parsed = await _read_upload(file)
    return await aindex_text(
        db=db,
        brand_id=brand_id,
        **parsed,
        document_id=document_id,
        team_id=member.team_id,
        actor_user_id=member.user_id,
        expected_version=expected_version,
    )


@router.delete("/documents/{document_id}")
async def remove_document(
    document_id: UUID,
    brand_id: UUID,
    expected_version: int = Query(..., ge=1),
    member: TeamMember = Depends(get_current_member_from_query),
    db: AsyncSession = Depends(get_async_db),
):
    await _get_brand_or_404(db, brand_id, team_id=member.team_id)
    await db.rollback()
    return await adelete_document(db, brand_id, document_id, expected_version, team_id=member.team_id, actor_user_id=member.user_id)


@router.post("/query")
async def query_rag(
    body: RagQuery,
    member: TeamMember = Depends(get_current_member_from_query),
    db: AsyncSession = Depends(get_async_db),
):
    brand = await _get_brand_or_404(db, body.brand_id, team_id=member.team_id)
    brand_id = brand.id
    # Vector retrieval has its own Session. Release the brand lookup connection
    # before retrieval and the potentially long external model request.
    await db.rollback()
    return await aask(body.question, brand_id, team_id=member.team_id, actor_user_id=member.user_id)


@router.get("/documents/{document_id}/structure")
async def get_document_structure(
    document_id: UUID, brand_id: UUID,
    member: TeamMember = Depends(get_current_member_from_query),
    db: AsyncSession = Depends(get_async_db),
):
    return await adocument_structure(db, brand_id, document_id, team_id=member.team_id, actor_user_id=member.user_id)


@router.get("/documents/{document_id}/contents/{content_id}")
async def get_document_content(
    document_id: UUID, content_id: str, brand_id: UUID,
    expected_version: int | None = Query(None, ge=1),
    member: TeamMember = Depends(get_current_member_from_query),
    db: AsyncSession = Depends(get_async_db),
):
    return await adocument_content(db, brand_id, document_id, content_id, expected_version=expected_version,
                                   team_id=member.team_id, actor_user_id=member.user_id)


@router.get("/documents/{document_id}/assets/{asset_id}")
async def get_document_asset(
    document_id: UUID, asset_id: str, brand_id: UUID,
    expected_version: int | None = Query(None, ge=1),
    member: TeamMember = Depends(get_current_member_from_query),
    db: AsyncSession = Depends(get_async_db),
):
    return await adocument_asset(db, brand_id, document_id, asset_id, expected_version=expected_version,
                                 team_id=member.team_id, actor_user_id=member.user_id)
