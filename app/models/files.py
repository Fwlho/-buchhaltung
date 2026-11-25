"""Gespeicherte Dateien (Logos, später Beleg-PDFs) – je Firma getrennt abgelegt."""
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TenantMixin


class StoredFile(TenantMixin, Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Ablagepfad relativ zu data/files/, z. B. "3/9f1c…e2.png"
    path: Mapped[str] = mapped_column(String(300), unique=True, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # Art der Datei: logo, pdf, zugferd, xrechnung, bild
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
