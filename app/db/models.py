from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Sempre grava/devolve datetimes com fuso (UTC), inclusive no SQLite dos testes."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime sem fuso horário")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class StatusConsulta:
    AGENDADA = "agendada"
    CONFIRMADA = "confirmada"
    REALIZADA = "realizada"
    CANCELADA = "cancelada"
    ATIVAS = (AGENDADA, CONFIRMADA)


class Paciente(Base):
    __tablename__ = "pacientes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telefone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    nome: Mapped[str | None] = mapped_column(String(200))
    chatwoot_contact_id: Mapped[int | None] = mapped_column(BigInteger)
    chatwoot_conversation_id: Mapped[int | None] = mapped_column(BigInteger)
    criado_em: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    consultas: Mapped[list[Consulta]] = relationship(back_populates="paciente")


class Consulta(Base):
    __tablename__ = "consultas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("pacientes.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(16))  # consulta | retorno
    inicio: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    fim: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default=StatusConsulta.AGENDADA, index=True)
    google_event_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    origem: Mapped[str] = mapped_column(String(16), default="bot")  # bot | agenda
    criado_em: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    paciente: Mapped[Paciente] = relationship(back_populates="consultas")


class Mensagem(Base):
    __tablename__ = "mensagens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("pacientes.id"), index=True)
    papel: Mapped[str] = mapped_column(String(16))  # paciente | bot | humano
    conteudo: Mapped[str] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class LembreteEnviado(Base):
    __tablename__ = "lembretes_enviados"
    __table_args__ = (UniqueConstraint("consulta_id", "tipo", name="uq_lembrete_consulta_tipo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consulta_id: Mapped[int] = mapped_column(ForeignKey("consultas.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(8))  # d-3 | d+3
    enviado_em: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
