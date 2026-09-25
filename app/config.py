"""Configuração: variáveis de ambiente (segredos) + config/clinic.yaml (dados do consultório)."""

from __future__ import annotations

import base64
import json
from datetime import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

LLM_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "groq": "https://api.groq.com/openai/v1",
}

WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://bot:bot@localhost:5432/bot"

    llm_provider: str = "gemini"
    llm_api_key: str = ""
    llm_model: str = "gemini-2.5-flash-lite"
    router_model: str = ""  # vazio = mesmo modelo do LLM_MODEL
    llm_base_url: str = ""

    chatwoot_url: str = ""
    chatwoot_account_id: int = 1
    chatwoot_api_token: str = ""
    chatwoot_inbox_id: int = 0
    chatwoot_bot_token: str = ""
    webhook_secret: str = ""

    google_service_account_json: str = ""
    google_calendar_id: str = ""

    scheduler_token: str = ""
    enable_internal_scheduler: bool = True
    doctor_alert_phone: str = ""

    tz: str = "America/Sao_Paulo"
    clinic_config_path: str = str(ROOT / "config" / "clinic.yaml")

    debounce_seconds: float = 4.0
    rate_limit_per_minute: int = 20
    history_limit: int = 20
    max_tool_iterations: int = 5
    log_level: str = "INFO"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def resolved_llm_base_url(self) -> str:
        if self.llm_base_url:
            return self.llm_base_url
        try:
            return LLM_BASE_URLS[self.llm_provider]
        except KeyError as exc:
            raise ValueError(f"LLM_PROVIDER inválido: {self.llm_provider}") from exc

    @property
    def async_database_url(self) -> str:
        """Aceita DATABASE_URL no formato postgres:// (RDS/Railway) e converte para asyncpg."""
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+asyncpg://" + url[len(prefix):]
        return url

    def google_credentials_info(self) -> dict:
        raw = self.google_service_account_json.strip()
        if not raw:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON não configurado")
        if not raw.startswith("{"):
            raw = base64.b64decode(raw).decode()
        return json.loads(raw)


class Atendimento(BaseModel):
    duracao_minutos: int = 60
    antecedencia_minima_horas: int = 24
    janela_maxima_dias: int = 60
    dias: dict[str, list[str]] = Field(default_factory=dict)

    def windows_for(self, weekday: int) -> list[tuple[time, time]]:
        """Janelas (início, fim) para o dia da semana (0 = segunda)."""
        result = []
        for spec in self.dias.get(WEEKDAYS[weekday], []) or []:
            start, end = spec.split("-")
            result.append((time.fromisoformat(start.strip()), time.fromisoformat(end.strip())))
        return result


class Retorno(BaseModel):
    prazo_dias: int = 45
    duracao_minutos: int = 60


class Lembretes(BaseModel):
    horario_execucao: str = "09:00"
    # Lembretes antes da consulta (ex.: [11, 3]: um antes do prazo de cancelamento e outro perto da data).
    dias_antes: list[int] = Field(default_factory=lambda: [3])

    @field_validator("dias_antes", mode="before")
    @classmethod
    def _lista(cls, v):
        return [v] if isinstance(v, int) else v
    dias_depois: int = 3


class Privacidade(BaseModel):
    retencao_mensagens_dias: int = 365
    aviso_primeiro_contato: str = ""


class Mensagens(BaseModel):
    """Respostas fixas: casos clínicos nunca recebem texto gerado pela IA."""

    clinico: str = (
        "Obrigada pela mensagem. Como se trata de uma questão de saúde, ela foi encaminhada "
        "para a equipe da Dra. Ana Paula, que vai responder assim que possível."
    )
    urgente: str = (
        "Recebemos sua mensagem e ela foi encaminhada com prioridade para a equipe da "
        "Dra. Ana Paula, que vai responder o mais rápido possível."
    )


class Templates(BaseModel):
    lembrete_consulta: str = "lembrete_consulta"
    pos_consulta: str = "pos_consulta"
    alerta_urgente: str = "alerta_urgente"
    idioma: str = "pt_BR"


class ClinicConfig(BaseModel):
    medica: str = "Dra. Ana Paula"
    especialidade: str = "nefrologista"
    cidade: str = "São Carlos (SP)"
    site: str = ""
    endereco: str = "[A DEFINIR]"
    formas_pagamento: str = "[A DEFINIR]"
    politica_cancelamento: str = "[A DEFINIR]"
    cancelamento_prazo_dias: int = 0
    valores: dict[str, str] = Field(default_factory=lambda: {"consulta": "R$ 500,00"})
    retorno: Retorno = Field(default_factory=Retorno)
    atendimento: Atendimento = Field(default_factory=Atendimento)
    lembretes: Lembretes = Field(default_factory=Lembretes)
    privacidade: Privacidade = Field(default_factory=Privacidade)
    templates: Templates = Field(default_factory=Templates)
    mensagens: Mensagens = Field(default_factory=Mensagens)

    def duracao(self, tipo: str) -> int:
        return self.retorno.duracao_minutos if tipo == "retorno" else self.atendimento.duracao_minutos


def load_clinic(path: str | Path) -> ClinicConfig:
    with open(path, encoding="utf-8") as fh:
        return ClinicConfig.model_validate(yaml.safe_load(fh) or {})


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_clinic() -> ClinicConfig:
    return load_clinic(get_settings().clinic_config_path)
