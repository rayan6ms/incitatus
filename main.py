import os
import io
import re
import json
import logging
import asyncio
import aiohttp
from aiohttp import web
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
from operator import itemgetter

import discord
from discord import app_commands, ButtonStyle, Interaction
from discord.ui import View, button, Button
from discord.app_commands import check
from discord.ext import commands, tasks
from dotenv import load_dotenv


load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_API_URL = "https://gameinfo.albiononline.com/api/gameinfo/guilds/29kSNYdrTv2iSPH0uHjbpQ/members"
MEMBERS_FILE = "members.json"
CONFIG_FILE = "config.json"
RANKING_FILE = "ranking.json"
BALANCES_FILE = "balances.json"
ALLIANCE_FILE = "alliance.json"
REG_FILE = "registrations.json"


@dataclass
class BotConfig:
    event_channel: Optional[int] = None
    participate_channel: Optional[int] = None
    event_log_channel: Optional[int] = None
    guild_channel: Optional[int] = None
    bot_log_channel: Optional[int] = None
    ranking_channel: Optional[int] = None
    split_channel: Optional[int] = None
    baltop_channel: Optional[int] = None
    info_channel: Optional[int] = None
    register_channel: Optional[int] = None

    event_msg_id: Optional[int] = None
    participate_msg_id: Optional[int] = None
    info_msg_id: Optional[int] = None
    ranking_msg_id: Optional[int] = None
    baltop_msg_id: Optional[int] = None
    register_msg_id: Optional[int] = None

    event_count: int = 0

    @classmethod
    def load(cls, path: str = CONFIG_FILE) -> "BotConfig":
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            filtered = {k: v for k, v in data.items() if k in cls.__annotations__}
            return cls(**filtered)
        return cls()

    def save(self, path: str = CONFIG_FILE) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)


try:
    from zoneinfo import ZoneInfo
    BR_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    BR_TZ = timezone(timedelta(hours=-3))


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


members_set = set(load_json(MEMBERS_FILE, []))
ranking = load_json(RANKING_FILE, {})
balances = load_json(BALANCES_FILE, {})
alliance_data = load_json(ALLIANCE_FILE, {"Guilds": []})
valid_guild_ids = {g["Id"] for g in alliance_data.get("Guilds", [])}
registrations: dict[str, int] = load_json(REG_FILE, {})
cleanup_jobs: dict[int, asyncio.Task] = {}

config = BotConfig.load()


def save_registrations():
    save_json(REG_FILE, registrations)


def cfg(name, default=None):
    return getattr(config, name, default)


intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix=commands.when_mentioned_or(""), intents=intents)


class DiscordLogHandler(logging.Handler):
    def emit(self, record):
        ch_id = cfg("bot_log_channel")
        if ch_id is None or bot.is_closed():
            return

        channel = bot.get_channel(ch_id)
        if channel is None:
            print("⚠️  Canal bot_log_channel não encontrado – log ignorado.")
            return

        msg = self.format(record)
        if len(msg) <= 1950:
            bot.loop.create_task(channel.send(f"```{msg}```"))
        else:
            bot.loop.create_task(
                channel.send(
                    "Stack-trace grande:",
                    file=discord.File(io.BytesIO(msg.encode()), "log.txt")
                )
            )


root = logging.getLogger()
root.setLevel(logging.INFO)
root.addHandler(logging.FileHandler("discord_bot.log", encoding="utf-8", mode="w"))
root.addHandler(DiscordLogHandler())
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


class EventState:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.event_channel_id = cfg.event_channel
        self.participate_channel_id = cfg.participate_channel
        self.log_channel_id = cfg.event_log_channel
        self.ranking_channel_id = cfg.ranking_channel
        self.register_channel_id = cfg.register_channel
        self.running = False
        self.count = cfg.event_count
        self.participants: set[int] = set()


state = EventState(config)


def persist(attr: str, val: int):
    setattr(config, attr, val)
    config.save()
    if attr.endswith("_channel"):
        setattr(state, f"{attr}_id", val)
    elif attr == "event_count":
        state.count = val


def save_balances():
    save_json(BALANCES_FILE, balances)


def format_fame(v: int) -> str:
    return (f"{v / 1_000_000_000:.2f} b" if v >= 1e9 else
            f"{v / 1_000_000:.2f} m" if v >= 1e6 else
            f"{v / 1_000:.2f} k" if v >= 1e3 else str(v))


def fmt_coin(n: int) -> str:
    return f"{n:,}".replace(",", ".")


async def fetch_player(nickname: str) -> dict | None:
    url = f"https://gameinfo.albiononline.com/api/gameinfo/search?q={nickname}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
            async with sess.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
        for p in data.get("players", []):
            if p["Name"].lower() == nickname.lower():
                return p
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    return None


async def purge_register_channel(channel: discord.TextChannel):
    anchor_id = getattr(config, "register_msg_id", None)

    async for msg in channel.history(limit=100):
        if msg.id == anchor_id:
            continue
        if msg.author != bot.user:
            try:
                await msg.delete()
            except discord.Forbidden:
                logging.warning("Sem permissão para deletar msg em register.")


class AdminView(View):
    def __init__(self, *, running: bool):
        super().__init__(timeout=None)
        for child in self.children:
            if child.custom_id == "evt_create":
                child.disabled = running
            else:
                child.disabled = not running
        self.running = running

    @button(label="Criar evento", emoji="✅", style=ButtonStyle.success, custom_id="evt_create")
    async def create(self, interaction: Interaction, button: Button):
        if not is_admin_or_senate(interaction.user) or self.running:
            return await interaction.response.defer()
        await start_event(interaction.guild)
        await interaction.response.edit_message(view=AdminView(running=True))

    @button(label="Encerrar", emoji="🛑", style=ButtonStyle.danger, custom_id="evt_end")
    async def end(self, interaction: Interaction, button: Button):
        if not is_admin_or_senate(interaction.user) or not self.running:
            return await interaction.response.defer()
        await finish_event(interaction.guild, cancelled=False)
        await interaction.response.edit_message(view=AdminView(running=False))

    @button(label="Cancelar", emoji="❌", style=ButtonStyle.secondary, custom_id="evt_cancel")
    async def cancel(self, interaction: Interaction, button: Button):
        if not is_admin_or_senate(interaction.user) or not self.running:
            return await interaction.response.defer()
        await finish_event(interaction.guild, cancelled=True)
        await interaction.response.edit_message(view=AdminView(running=False))


class ParticipateView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="Participar", emoji="✅", style=ButtonStyle.success, custom_id="join_evt")
    async def join(self, interaction: Interaction, button: Button):
        if not state.running:
            return await interaction.response.defer()
        state.participants.add(interaction.user.id)
        await ensure_participate_msg(interaction.guild)
        await interaction.response.defer(ephemeral=True)

    @button(label="Sair", emoji="⛔", style=ButtonStyle.danger, custom_id="leave_evt")
    async def leave(self, interaction: Interaction, button: Button):
        state.participants.discard(interaction.user.id)
        await ensure_participate_msg(interaction.guild)
        await interaction.response.defer(ephemeral=True)


PARTICIPATE_VIEW: ParticipateView | None = None


async def ensure_message(
    *, channel: discord.TextChannel,
    stored_id: Optional[int],
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> int:
    if stored_id:
        try:
            msg = await channel.fetch_message(stored_id)
            await msg.edit(content=content, embed=embed, view=view)
            return stored_id
        except discord.NotFound:
            pass

    msg = await channel.send(content=content, embed=embed, view=view)
    return msg.id


async def ensure_event_msg(guild: discord.Guild):
    ch = guild.get_channel(config.event_channel)
    if not ch:
        return
    config.event_msg_id = await ensure_message(
        channel=ch,
        stored_id=config.event_msg_id,
        content=f"**Evento #{state.count}**" if state.running else "**Nenhum evento no momento**",
        view=AdminView(running=state.running),
    )
    config.save()


async def ensure_participate_msg(guild: discord.Guild):
    global PARTICIPATE_VIEW

    ch = guild.get_channel(config.participate_channel)
    if not ch:
        return
    if state.running:
        if PARTICIPATE_VIEW is None:
            PARTICIPATE_VIEW = ParticipateView()

        view = PARTICIPATE_VIEW
        names = [(m.display_name if (m := guild.get_member(u)) else f'<{u}>') for u in state.participants]
        content = f"🎉 **Evento #{state.count} aberto!**\n" \
            f"Participantes ({len(names)}): {', '.join(names) or 'ninguém ainda'}"
    else:
        view, content = None, "Nenhum evento no momento."

    config.participate_msg_id = await ensure_message(
        channel=ch,
        stored_id=config.participate_msg_id,
        content=content,
        view=view,
    )
    state.participate_message_id = config.participate_msg_id
    config.save()


async def ensure_info_msg(guild: discord.Guild, member_count: int):
    if not (ch := guild.get_channel(config.info_channel)):
        return

    content = f"👥 **Membros atuais da guild:** **{member_count}**"

    config.info_msg_id = await ensure_message(
        channel=ch,
        stored_id=config.info_msg_id,
        content=content,
    )
    config.save()


async def ensure_ranking_msg(guild: discord.Guild, *, top_n: int = 10):
    if not (ch := guild.get_channel(config.ranking_channel)):
        return
    if not ranking:
        return

    medals = ["🥇", "🥈", "🥉"]
    top = sorted(ranking.items(), key=itemgetter(1), reverse=True)[:top_n]

    lines = []
    for idx, (uid, count) in enumerate(top, 1):
        icon = medals[idx - 1] if idx <= len(medals) else "🏅"
        member = guild.get_member(int(uid))
        name = member.display_name if member else f"<{uid}>"
        lines.append(f"{icon} {idx}. **{name}** — {count}")

    embed = discord.Embed(
        title="🏆 Ranking de Participação",
        description="\n".join(lines),
        color=0xf1c40f,
    )

    config.ranking_msg_id = await ensure_message(
        channel=ch,
        stored_id=config.ranking_msg_id,
        embed=embed,
    )
    config.save()


async def ensure_baltop_msg(guild: discord.Guild, *, top_n: int = 10):
    if not (ch := guild.get_channel(config.baltop_channel)):
        return
    if not balances:
        return

    medals = ["🥇", "🥈", "🥉"]
    top = sorted(balances.items(), key=itemgetter(1), reverse=True)[:top_n]

    lines = []
    for idx, (uid, bal) in enumerate(top, 1):
        icon = medals[idx - 1] if idx <= len(medals) else "🏅"
        member = guild.get_member(int(uid))
        name = member.display_name if member else f"<{uid}>"
        lines.append(f"{icon} {idx}. **{name}** — `{fmt_coin(bal)}`")

    embed = discord.Embed(
        title="💰 Top Saldos",
        description="\n".join(lines),
        color=0x95a5a6,
    )

    config.baltop_msg_id = await ensure_message(
        channel=ch,
        stored_id=config.baltop_msg_id,
        embed=embed,
    )
    config.save()


async def ensure_register_msg(guild: discord.Guild):
    if not (ch := guild.get_channel(config.register_channel)):
        return
    content = (
        "🎯 **Registro de membros**\n"
        "Use o comando `/register <seu-nome-do-albion>` para liberar o acesso ao servidor."
    )
    config.register_msg_id = await ensure_message(
        channel=ch,
        stored_id=config.register_msg_id,
        content=content,
    )
    config.save()


def is_admin_or_senate(member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    return any(r.name.lower() == "senate" for r in member.roles)


def admin_or_senate_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_admin_or_senate(interaction.user)
    return check(predicate)


async def fetch_last_event_number(guild: discord.Guild) -> int:
    if state.log_channel_id is None:
        return 0
    ch = guild.get_channel(state.log_channel_id)
    if ch is None:
        return 0
    async for m in ch.history(limit=20):
        if m.author == bot.user and m.attachments:
            match = re.search(r"evento_(\d+)\.txt", m.attachments[0].filename)
            if match:
                return int(match.group(1))
    return 0


def make_set_channel(cmd, key, label):
    @bot.tree.command(name=cmd, description=f"Define {label}")
    @app_commands.describe(channel="Canal")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def _set(inter: discord.Interaction, channel: discord.TextChannel):
        persist(key, channel.id)
        await inter.response.send_message(f"{label} definido: {channel.mention}", ephemeral=True)
        if key == "event_channel":
            await ensure_event_msg(inter.guild)
        if key == "participate_channel":
            await ensure_participate_msg(inter.guild)
        if key == "info_channel":
            await ensure_info_msg(inter.guild, len(members_set))
        if key == "ranking_channel":
            await ensure_ranking_msg(inter.guild)
        if key == "baltop_channel":
            await ensure_baltop_msg(inter.guild)
        if key == "register_channel":
            await ensure_register_msg(inter.guild)

    return _set


for c, k, l in [("set-event-channel", "event_channel", "canal de eventos"),
                ("set-participate-channel", "participate_channel", "canal de participação"),
                ("set-eventlog-channel", "event_log_channel", "canal de event-log"),
                ("set-guild-channel", "guild_channel", "canal de entrada e saída"),
                ("set-botlog-channel", "bot_log_channel", "canal de logs do bot"),
                ("set-ranking-channel", "ranking_channel", "canal de ranking"),
                ("set-split-channel", "split_channel", "canal de balanço"),
                ("set-baltop-channel", "baltop_channel", "canal de baltop"),
                ("set-info-channel", "info_channel", "canal de info da guild"),
                ("set-register-channel", "register_channel", "canal de registro de membros")]:
    make_set_channel(c, k, l)


async def start_event(guild):
    last_num = await fetch_last_event_number(guild)
    state.count = last_num + 1
    persist("event_count", state.count)
    state.running = True
    state.participants = set()

    await ensure_event_msg(guild)
    await ensure_participate_msg(guild)
    logging.info(f"Evento #{state.count} iniciado ✅")


async def finish_event(guild, *, cancelled=False):
    eid = state.count
    state.running = False
    await ensure_event_msg(guild)
    await ensure_participate_msg(guild)
    config.save()

    if not cancelled and state.participants and state.log_channel_id:
        log_ch = guild.get_channel(state.log_channel_id)
        txt = (f"Evento #{eid} - {len(state.participants)} participantes\n" +
               "\n".join((guild.get_member(u).display_name if guild.get_member(u) else f"<{u}>") for u in state.participants))
        await log_ch.send(file=discord.File(io.BytesIO(txt.encode()), filename=f"evento_{eid}.txt"))

        for uid in state.participants:
            ranking[str(uid)] = ranking.get(str(uid), 0) + 1
        save_json(RANKING_FILE, ranking)
        await ensure_ranking_msg(guild)

    state.participants.clear()
    emoji = "❌" if cancelled else "🛑"
    logging.info(f"Evento #{eid} {'cancelado' if cancelled else 'encerrado'} {emoji}")


@tasks.loop(minutes=30)
async def check_new_members():
    ch_id = cfg("guild_channel")
    if ch_id is None:
        logging.warning("guild_channel não configurado.")
        return

    channel = bot.get_channel(ch_id)
    if channel is None:
        logging.warning("Canal guild_channel não encontrado.")
        return

    data = None
    for attempt, delay in enumerate((0, 2, 4), 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
                async with sess.get(GUILD_API_URL) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            break
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.warning(f"[Albion API] tentativa {attempt}/3 falhou ({e})")
    if data is None:
        logging.warning("[Albion API] 3 tentativas falharam; abortando rodada.")
        return

    current_names = {m["Name"] for m in data}
    joined_names = current_names - members_set
    left_names = members_set - current_names

    members_by_name = {m["Name"]: m for m in data}

    for name in joined_names:
        m = members_by_name[name]
        pvp = m["KillFame"]
        pve = m["LifetimeStatistics"]["PvE"]["Total"]
        total = pvp + pve

        emb = (discord.Embed(title=f"Bem-vindo(a) {name}!", color=0x3498db)
               .add_field(name="🏹 Fama PvP", value=format_fame(pvp), inline=True)
               .add_field(name="🧌 Fama PvE", value=format_fame(pve), inline=True)
               .add_field(name="🏆 Fama Total", value=format_fame(total), inline=False)
               .set_footer(text=datetime.now(BR_TZ).strftime("Ingressou em: %d/%m/%Y %H:%M")))

        await channel.send(embed=emb)

    for name in left_names:
        bye = discord.Embed(
            title=f"{name} deixou a guilda.",
            description="Romani ite domum!",
            color=0xe74c3c
        )
        await channel.send(embed=bye)

    members_set.clear()
    members_set.update(current_names)
    save_json(MEMBERS_FILE, list(members_set))

    await ensure_info_msg(channel.guild, len(current_names))


@bot.tree.command(name="split-loot", description="Divide o loot entre os participantes do evento atual.")
@app_commands.guild_only()
@admin_or_senate_check()
@app_commands.describe(total="Valor total (obrigatório, >0)",
                       tax="Taxa da guild (0-100%)",
                       repair="Custos de reparo (>=0)")
async def split_loot(inter: discord.Interaction,
                     total: app_commands.Range[int, 1, None],
                     tax: app_commands.Range[int, 0, 100] = 0,
                     repair: app_commands.Range[int, 0, None] = 0):
    if not state.running:
        await inter.response.send_message("❌ Nenhum evento ativo.", ephemeral=True)
        return
    if total <= 0:
        return await inter.response.send_message("❌ total deve ser > 0", ephemeral=True)
    if tax < 0 or tax > 100:
        return await inter.response.send_message("❌ taxa 0-100 %", ephemeral=True)
    if repair < 0:
        return await inter.response.send_message("❌ reparo ≥ 0", ephemeral=True)
    if not (n := len(state.participants)):
        return await inter.response.send_message(
            "❌ Nenhum participante.", ephemeral=True
        )
    guild_cut = total * tax // 100
    restante = total - guild_cut - repair
    if restante < 0:
        await inter.response.send_message("❌ Taxa + reparo maiores que o total.", ephemeral=True)
        return

    per_head = restante // n
    sobra = restante - per_head * n

    guild_cut += sobra

    lines = [
        f"**Evento #{state.count}**",
        f"Total: `{fmt_coin(total)}`",
        f"Taxa guilda ({tax}%): `{fmt_coin(guild_cut)}`" if tax or sobra else "",
        f"Reparo: `{fmt_coin(repair)}`" if repair else "",
        "",
    ]

    for uid in state.participants:
        balances[str(uid)] = balances.get(str(uid), 0) + per_head
        member = inter.guild.get_member(uid)
        lines.append(f"• **{member.display_name}** → `{fmt_coin(per_head)}`")

    save_json(BALANCES_FILE, balances)
    await ensure_baltop_msg(inter.guild)

    out = "\n".join(line for line in lines if line)
    ch_id = cfg("split_channel")
    if ch_id is None:
        logging.warning("split_channel não configurado.")
        target = inter.channel
    else:
        target = bot.get_channel(ch_id)
    await target.send(out)
    await inter.response.send_message("✅ Split registrado!", ephemeral=True)


@bot.tree.command(name="balance", description="Exibe seu saldo ou o de outro jogador.")
@app_commands.describe(user="Jogador (mencione ou deixe em branco para você)")
async def balance_cmd(inter: discord.Interaction, user: discord.Member = None):
    user = user or inter.user
    val = balances.get(str(user.id), 0)
    await inter.response.send_message(f"💰 **Saldo de {user.display_name}:** `{fmt_coin(val)}`")


@bot.tree.command(name="pay", description="Registra pagamento (reduz saldo) de um jogador.")
@app_commands.guild_only()
@admin_or_senate_check()
@app_commands.describe(user="Jogador", value="Valor pago (inteiro >0)")
async def pay_cmd(inter: discord.Interaction,
                  user: discord.Member,
                  value: app_commands.Range[int, 1, None]):
    uid = str(user.id)
    current = balances.get(uid, 0)
    if current == 0:
        return await inter.response.send_message(
            f"{user.display_name} não possui saldo pendente.", ephemeral=True)
    if value > current:
        return await inter.response.send_message(
            f"Saldo de {fmt_coin(current)} é menor que o valor informado.", ephemeral=True)

    balances[uid] = current - value
    save_balances()

    await inter.response.send_message(
        f"✅ Registrado pagamento de `{fmt_coin(value)}` para {user.display_name}.\n"
        f"Saldo restante: `{fmt_coin(balances[uid])}`", ephemeral=True)

    await ensure_baltop_msg(inter.guild)


@bot.tree.command(
    name="register",
    description="Vincula seu nick do Albion e libera o acesso ao servidor.",
)
@app_commands.describe(nickname="Digite exatamente como aparece no jogo")
@app_commands.guild_only()
@app_commands.checks.cooldown(2, 30)
async def register_cmd(inter: Interaction, nickname: str):
    if config.register_channel and inter.channel_id != config.register_channel:
        return await inter.response.send_message(
            f"Use o comando em <#{config.register_channel}>.", ephemeral=True
        )

    plebs = discord.utils.get(inter.guild.roles, name="Plebs")
    if plebs and plebs in inter.user.roles:
        return await inter.response.send_message(
            "Você já está registrado. Use `/unregister` se precisar alterar seus dados.",
            ephemeral=True,
        )

    nick_lower = nickname.lower().strip()
    other_id = registrations.get(nick_lower)
    if other_id and other_id != inter.user.id:
        return await inter.response.send_message(
            "Este nickname já foi registrado por outro membro.\n"
            "Se acredita que há um engano, contate a moderação (@senate ou @caesar).",
            ephemeral=True,
        )

    if other_id == inter.user.id:
        return await inter.response.send_message(
            "Você já está registrado com esse nickname. "
            "Use `/unregister` se precisar alterar.",
            ephemeral=True,
        )

    await inter.response.defer(ephemeral=True, thinking=True)

    player = await fetch_player(nickname)
    if not player:
        return await inter.followup.send(
            "Não encontrei esse nick na API do Albion. "
            "Verifique se digitou **exatamente** (maiúsculas/minúsculas não importam).",
            ephemeral=True,
        )

    gid = player.get("GuildId")
    gname = player.get("GuildName") or "-----"

    if not gid:
        return await inter.followup.send(
            "Você não está em nenhuma guild no momento. Entre em uma guild válida e tente novamente.",
            ephemeral=True,
        )

    if gid not in valid_guild_ids:
        return await inter.followup.send(
            f"Sua guild (**{gname}**) não é autorizada.\n"
            "Se você acredita que houve um engano, "
            "por favor mencione a moderação (@senate ou @caesar).",
            ephemeral=True,
        )

    if plebs:
        try:
            await inter.user.add_roles(plebs, reason="Registro automático")
        except discord.Forbidden:
            return await inter.followup.send(
                "Não tenho permissão para adicionar cargos. Avise a moderação (@senate ou @caesar).",
                ephemeral=True,
            )

    tag = gname.upper()[:5]
    new_nick = f"[{tag}] {player['Name']}"
    try:
        await inter.user.edit(nick=new_nick, reason="Registro automático")
        nick_msg = f"Seu apelido foi alterado para **{new_nick}**."
    except discord.Forbidden:
        nick_msg = "Não consegui alterar seu apelido (permissão faltando)."

    registrations[nick_lower] = inter.user.id
    save_registrations()

    await inter.followup.send(f"✅ Registro concluído! {nick_msg}", ephemeral=True)


@register_cmd.error
async def register_error(inter: Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        retry = f"{error.retry_after:.1f}".rstrip("0").rstrip(".")
        await inter.response.send_message(
            f"⏳ Você usou esse comando recentemente. Tente novamente em **{retry} s**.",
            ephemeral=True
        )
    else:
        raise error


@bot.tree.command(
    name="unregister",
    description="Remove o registro.",
)
@app_commands.guild_only()
async def unregister_cmd(inter: Interaction):
    plebs = discord.utils.get(inter.guild.roles, name="Plebs")

    if not plebs or plebs not in inter.user.roles:
        return await inter.response.send_message(
            "Você ainda não está registrado.", ephemeral=True
        )

    for n, uid in list(registrations.items()):
        if uid == inter.user.id:
            del registrations[n]
            save_registrations()
            break
    try:
        await inter.user.remove_roles(plebs, reason="Auto-unregister")
    except discord.Forbidden:
        return await inter.response.send_message(
            "Não consegui remover o cargo (permissão faltando).", ephemeral=True
        )

    try:
        await inter.user.edit(nick=None, reason="Auto-unregister")
    except discord.Forbidden:
        pass

    await inter.response.send_message("✅ Registro removido. Você pode registrar novamente.", ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    if (
        message.guild is None or
        message.author == bot.user or
        message.channel.id != config.register_channel
    ):
        return

    job = cleanup_jobs.pop(message.channel.id, None)
    if job and not job.done():
        job.cancel()

    async def delayed_cleanup():
        try:
            await asyncio.sleep(300)
            await purge_register_channel(message.channel)
        except asyncio.CancelledError:
            pass

    cleanup_jobs[message.channel.id] = bot.loop.create_task(delayed_cleanup())


@bot.event
async def on_ready():
    global PARTICIPATE_VIEW
    if PARTICIPATE_VIEW is None:
        PARTICIPATE_VIEW = ParticipateView()

    bot.add_view(AdminView(running=False))
    bot.add_view(PARTICIPATE_VIEW)

    await bot.tree.sync()

    for guild in bot.guilds:
        await ensure_event_msg(guild)
        await ensure_participate_msg(guild)
        await ensure_info_msg(guild, len(members_set))
        await ensure_ranking_msg(guild)
        await ensure_baltop_msg(guild)
        await ensure_register_msg(guild)

    if not check_new_members.is_running():
        check_new_members.start()

    logging.info(f"Bot online como {bot.user}")


async def start_web_server():
    async def handle(request):
        return web.Response(text="Incitatus está vivo.")

    app = web.Application()
    app.add_routes([web.get("/", handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000)))
    await site.start()


async def main():
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
