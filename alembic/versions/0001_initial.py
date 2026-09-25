"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""
import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "pacientes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("telefone", sa.String(32), nullable=False),
        sa.Column("nome", sa.String(200)),
        sa.Column("chatwoot_contact_id", sa.BigInteger),
        sa.Column("chatwoot_conversation_id", sa.BigInteger),
        sa.Column("criado_em", TS, nullable=False),
    )
    op.create_index("ix_pacientes_telefone", "pacientes", ["telefone"], unique=True)

    op.create_table(
        "consultas",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("paciente_id", sa.Integer, sa.ForeignKey("pacientes.id"), nullable=False),
        sa.Column("tipo", sa.String(16), nullable=False),
        sa.Column("inicio", TS, nullable=False),
        sa.Column("fim", TS, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("google_event_id", sa.String(255), unique=True),
        sa.Column("origem", sa.String(16), nullable=False, server_default="bot"),
        sa.Column("criado_em", TS, nullable=False),
    )
    op.create_index("ix_consultas_paciente_id", "consultas", ["paciente_id"])
    op.create_index("ix_consultas_inicio", "consultas", ["inicio"])
    op.create_index("ix_consultas_status", "consultas", ["status"])

    op.create_table(
        "mensagens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("paciente_id", sa.Integer, sa.ForeignKey("pacientes.id"), nullable=False),
        sa.Column("papel", sa.String(16), nullable=False),
        sa.Column("conteudo", sa.Text, nullable=False),
        sa.Column("criado_em", TS, nullable=False),
    )
    op.create_index("ix_mensagens_paciente_id", "mensagens", ["paciente_id"])
    op.create_index("ix_mensagens_criado_em", "mensagens", ["criado_em"])

    op.create_table(
        "lembretes_enviados",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("consulta_id", sa.Integer, sa.ForeignKey("consultas.id"), nullable=False),
        sa.Column("tipo", sa.String(8), nullable=False),
        sa.Column("enviado_em", TS, nullable=False),
        sa.UniqueConstraint("consulta_id", "tipo", name="uq_lembrete_consulta_tipo"),
    )
    op.create_index("ix_lembretes_enviados_consulta_id", "lembretes_enviados", ["consulta_id"])


def downgrade() -> None:
    op.drop_table("lembretes_enviados")
    op.drop_table("mensagens")
    op.drop_table("consultas")
    op.drop_table("pacientes")
