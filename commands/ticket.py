"""/ticket command -- ticket system (order / service / customer service).

Persistent panel view (timeout=None, static custom_id) survives bot restarts.
All interaction responses ephemeral -- ticket flow stays private to the user
clicking, admin actions stay private to the admin.
"""
import re
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import log_command_activity
from utils.products import (
    auto_whitelist_product_for_user,
    build_product_delivery_dm,
    get_product,
    give_product_to_user,
    list_product_types,
    list_products_by_type,
)
from utils.tickets import (
    claim_ticket_create_lock,
    close_ticket,
    create_ticket,
    find_open_ticket,
    get_order_selection,
    get_ticket,
    get_ticket_categories,
    get_testi_channel,
    next_ticket_number,
    mark_ticket_deleted,
    release_ticket_create_lock,
    save_order_selection,
    set_testi_channel,
    set_ticket_categories,
)
from utils.verification import get_verified_user

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SELECT_OPTIONS = 25

CID_PANEL_CATEGORY_SELECT = 'ticket_panel_category'
CID_ORDER_PRODUCT_SELECT = 'ticket_order_products'
CID_ORDER_CREATE_BTN_PREFIX = 'ticket_order_create'  # ticket_order_create_{token}
CID_SERVICE_OPEN_MODAL_BTN = 'ticket_service_open_modal'
CID_CS_CREATE_BTN = 'ticket_cs_create'

CATEGORY_LABELS = {
    'order': 'Order',
    'service': 'Service',
    'customerservice': 'Customer Service',
}

COLOR_SUCCESS = 0x57F287
COLOR_INFO = 0x5865F2

_DIGITS_RE = re.compile(r'[^0-9]')


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _format_idr(n) -> str:
    return f'Rp{int(n):,}'.replace(',', '.')


def _parse_price(price_str) -> int:
    digits = _DIGITS_RE.sub('', str(price_str))
    return int(digits) if digits else 0


def _require_admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.administrator)


async def _deny_not_admin(interaction: discord.Interaction):
    await interaction.response.send_message(
        '🔒 You need **Administrator** permission to do that.', ephemeral=True
    )


def _info_embed(title: str, description: str = '', color: int = COLOR_INFO) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


async def _load_products_map(product_ids: list[str]) -> dict:
    result = {}
    for pid in product_ids:
        p = await get_product(pid)
        if p:
            result[pid] = p
    return result


async def _create_ticket_channel(interaction: discord.Interaction, category: str) -> discord.TextChannel:
    guild = interaction.guild
    admin_roles = [r for r in guild.roles if r.permissions.administrator]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        ),
    }
    for role in admin_roles:
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )

    channel_name = f'{category}-{interaction.user.name}'[:90]

    categories = await get_ticket_categories(str(interaction.guild_id))
    parent_id = (categories or {}).get(category)
    parent = guild.get_channel(int(parent_id)) if parent_id else None

    return await guild.create_text_channel(
        channel_name,
        category=parent,
        overwrites=overwrites,
        topic=f'{CATEGORY_LABELS[category]} ticket for {interaction.user}',
    )


async def _guard_duplicate_open_ticket(interaction: discord.Interaction, category: str) -> discord.TextChannel | None:
    """Returns an existing open ticket channel mention message if one
    exists, otherwise None. Caller should bail out on non-None.
    """
    existing = await find_open_ticket(str(interaction.guild_id), str(interaction.user.id), category)
    return existing


async def _claim_lock_or_bail(interaction: discord.Interaction, category: str) -> bool:
    """Acquire the double-click create lock. If another open ticket appears
    to already exist for this user+category, release the lock and report it.
    Returns True if the caller should proceed with channel creation.
    """
    got_lock = await claim_ticket_create_lock(str(interaction.user.id), category)
    if got_lock:
        existing = await find_open_ticket(str(interaction.guild_id), str(interaction.user.id), category)
        if existing:
            await release_ticket_create_lock(str(interaction.user.id), category)
            await interaction.edit_original_response(
                content=f"⚠️ You already have an open ticket: <#{existing['channelId']}>", embed=None, view=None
            )
            return False
    return True


async def _finish_or_discard_duplicate(
    interaction: discord.Interaction, channel: discord.TextChannel, got_lock: bool
) -> bool:
    """If the lock wasn't acquired (double-click race lost), delete the
    freshly created duplicate channel and inform the user. Returns True if
    the caller should continue (lock held), False if it bailed.
    """
    if got_lock:
        return True

    try:
        await channel.delete(reason='Duplicate ticket from double-click')
    except discord.HTTPException:
        pass

    await interaction.edit_original_response(
        content="⚠️ That got triggered twice -- your ticket was already created, check your channel list.",
        embed=None,
        view=None,
    )
    return False


# ---------------------------------------------------------------------------
# Persistent ticket panel view -- sent by /ticket send, survives restarts.
# ---------------------------------------------------------------------------
class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id=CID_PANEL_CATEGORY_SELECT,
        placeholder='📋 Select a ticket category',
        options=[
            discord.SelectOption(label='Order', value='order', description='Buy a product', emoji='🛒'),
            discord.SelectOption(label='Service', value='service', description='Request a service', emoji='🛠️'),
            discord.SelectOption(label='Customer Service', value='customerservice', description='Talk to an admin', emoji='💬'),
        ],
    )
    async def on_category_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        # Panel components bypass the slash-command tree's interaction_check,
        # so the verification gate is re-applied here explicitly.
        verified_record = await get_verified_user(str(interaction.user.id))
        if not verified_record:
            return await interaction.response.send_message(
                '🔒 You need to verify your Roblox account before opening a ticket. Run `/verify start` first.',
                ephemeral=True,
            )

        category = select.values[0]
        handler = {
            'order': self._handle_order,
            'service': self._handle_service,
            'customerservice': self._handle_customerservice,
        }[category]
        await handler(interaction)

    async def _handle_order(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        existing = await _guard_duplicate_open_ticket(interaction, 'order')
        if existing:
            return await interaction.followup.send(
                f"⚠️ You already have an open order ticket: <#{existing['channelId']}>", ephemeral=True
            )

        types = await list_product_types(str(interaction.guild_id))
        all_products = []
        for t in types:
            all_products.extend(await list_products_by_type(str(interaction.guild_id), t['id']))

        if not all_products:
            return await interaction.followup.send('📭 No products are available right now.', ephemeral=True)

        verified_user = await get_verified_user(str(interaction.user.id))
        owned = set((verified_user or {}).get('ownedProducts') or [])
        purchasable = [p for p in all_products if p['id'] not in owned]

        if not purchasable:
            return await interaction.followup.send('✅ You already own every available product.', ephemeral=True)

        options = [
            discord.SelectOption(
                label=p['name'][:100],
                description=f"{p['type']} — {p['price']}"[:100],
                value=p['id'],
            )
            for p in purchasable[:MAX_SELECT_OPTIONS]
        ]

        order_select = discord.ui.Select(
            custom_id=CID_ORDER_PRODUCT_SELECT,
            placeholder='🛒 Select product(s) to buy',
            min_values=1,
            max_values=len(options),
            options=options,
        )
        order_select.callback = _make_order_select_callback(order_select)
        order_view = discord.ui.View(timeout=None)
        order_view.add_item(order_select)

        await interaction.followup.send(
            '🛒 Pick the product(s) you want to buy (already-owned products are hidden):',
            view=order_view,
            ephemeral=True,
        )

    async def _handle_service(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        existing = await _guard_duplicate_open_ticket(interaction, 'service')
        if existing:
            return await interaction.followup.send(
                f"⚠️ You already have an open service ticket: <#{existing['channelId']}>", ephemeral=True
            )

        btn = discord.ui.Button(
            custom_id=CID_SERVICE_OPEN_MODAL_BTN, label='Fill service request', style=discord.ButtonStyle.primary, emoji='🛠️'
        )
        btn.callback = _service_open_modal_callback
        view = discord.ui.View(timeout=None)
        view.add_item(btn)

        await interaction.followup.send('🛠️ Click below to describe the service you need:', view=view, ephemeral=True)

    async def _handle_customerservice(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        existing = await _guard_duplicate_open_ticket(interaction, 'customerservice')
        if existing:
            return await interaction.followup.send(
                f"⚠️ You already have an open customer service ticket: <#{existing['channelId']}>", ephemeral=True
            )

        btn = discord.ui.Button(
            custom_id=CID_CS_CREATE_BTN, label='Create ticket', style=discord.ButtonStyle.primary, emoji='💬'
        )
        btn.callback = _cs_create_callback
        view = discord.ui.View(timeout=None)
        view.add_item(btn)

        await interaction.followup.send('💬 Click below to open a customer service ticket:', view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# Order flow: product select -> summary+create button -> channel
# ---------------------------------------------------------------------------

def _make_order_select_callback(select_widget: discord.ui.Select):
    async def _callback(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        product_ids = select_widget.values
        products_map = await _load_products_map(product_ids)
        valid_ids = [pid for pid in product_ids if pid in products_map]

        if not valid_ids:
            return await interaction.edit_original_response(
                content='❌ Selected product(s) no longer exist. Try again.', view=None
            )

        total = sum(_parse_price(products_map[pid]['price']) for pid in valid_ids)

        token = str(uuid.uuid4())
        await save_order_selection(token, str(interaction.user.id), valid_ids)

        embed = _info_embed('🛒 Order Summary')
        for pid in valid_ids:
            embed.add_field(name=products_map[pid]['name'], value=products_map[pid]['price'], inline=False)
        embed.add_field(name='Total', value=f'**{_format_idr(total)}**', inline=False)

        create_btn = discord.ui.Button(
            custom_id=f'{CID_ORDER_CREATE_BTN_PREFIX}_{token}',
            label='Create ticket',
            style=discord.ButtonStyle.success,
            emoji='✅',
        )
        create_btn.callback = _order_create_callback
        view = discord.ui.View(timeout=None)
        view.add_item(create_btn)

        await interaction.edit_original_response(content=None, embed=embed, view=view)

    return _callback


async def _order_create_callback(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    token = interaction.data['custom_id'].replace(f'{CID_ORDER_CREATE_BTN_PREFIX}_', '')
    selection = await get_order_selection(token)

    if not selection or selection['userId'] != str(interaction.user.id):
        return await interaction.edit_original_response(
            content='⌛ This selection has expired. Please start over from the ticket panel.', embed=None, view=None
        )

    product_ids = selection['productIds']

    got_lock = await claim_ticket_create_lock(str(interaction.user.id), 'order')
    if not await _claim_lock_or_bail_wrapper(interaction, 'order', got_lock):
        return

    products_map = await _load_products_map(product_ids)
    line_items = [
        {
            'productId': pid,
            'name': products_map[pid]['name'],
            'price': products_map[pid]['price'],
            'lineTotal': _parse_price(products_map[pid]['price']),
        }
        for pid in product_ids
        if pid in products_map
    ]
    total = sum(li['lineTotal'] for li in line_items)

    channel = await _create_ticket_channel(interaction, 'order')

    if not await _finish_or_discard_duplicate(interaction, channel, got_lock):
        return

    await create_ticket({
        'guildId': str(interaction.guild_id),
        'channelId': str(channel.id),
        'category': 'order',
        'creatorId': str(interaction.user.id),
        'products': line_items,
        'total': total,
    })
    await release_ticket_create_lock(str(interaction.user.id), 'order')

    embed = discord.Embed(title='🛒 New Order Ticket', color=COLOR_SUCCESS)
    for li in line_items:
        embed.add_field(name=li['name'], value=_format_idr(li['lineTotal']), inline=False)
    embed.add_field(name='Total', value=f"**{_format_idr(total)}**", inline=False)
    embed.set_footer(text=f'Requested by {interaction.user}')

    await channel.send(content=interaction.user.mention, embed=embed)

    await log_command_activity(
        interaction, subcommand='send', success=True,
        fields={'discordUser': interaction.user}, note=f'Order ticket created: {channel.id}, total {total}',
    )

    await interaction.edit_original_response(content=f'✅ Ticket created: {channel.mention}', embed=None, view=None)


async def _claim_lock_or_bail_wrapper(interaction: discord.Interaction, category: str, got_lock: bool) -> bool:
    """Same duplicate-guard logic as _claim_lock_or_bail but takes an
    already-claimed lock result (order flow claims the lock before this
    check runs). Returns True if caller should proceed.
    """
    if got_lock:
        existing = await find_open_ticket(str(interaction.guild_id), str(interaction.user.id), category)
        if existing:
            await release_ticket_create_lock(str(interaction.user.id), category)
            await interaction.edit_original_response(
                content=f"⚠️ You already have an open ticket: <#{existing['channelId']}>", embed=None, view=None
            )
            return False
    return True


# ---------------------------------------------------------------------------
# Service flow: button -> modal -> channel
# ---------------------------------------------------------------------------
class ServiceModal(discord.ui.Modal, title='Service Request'):
    service_answer = discord.ui.TextInput(
        label='What type of service do you want?', style=discord.TextStyle.paragraph, required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        got_lock = await claim_ticket_create_lock(str(interaction.user.id), 'service')
        if not await _claim_lock_or_bail_wrapper(interaction, 'service', got_lock):
            return

        answer = self.service_answer.value.strip()
        channel = await _create_ticket_channel(interaction, 'service')

        if not await _finish_or_discard_duplicate(interaction, channel, got_lock):
            return

        await create_ticket({
            'guildId': str(interaction.guild_id),
            'channelId': str(channel.id),
            'category': 'service',
            'creatorId': str(interaction.user.id),
            'serviceAnswer': answer,
        })
        await release_ticket_create_lock(str(interaction.user.id), 'service')

        embed = discord.Embed(title='🛠️ New Service Ticket', color=COLOR_INFO)
        embed.add_field(name='Requested service', value=answer[:1024], inline=False)
        embed.set_footer(text=f'Requested by {interaction.user}')

        await channel.send(content=interaction.user.mention, embed=embed)
        await interaction.edit_original_response(content=f'✅ Ticket created: {channel.mention}', embed=None, view=None)


async def _service_open_modal_callback(interaction: discord.Interaction):
    await interaction.response.send_modal(ServiceModal())


# ---------------------------------------------------------------------------
# Customer service flow: button -> channel
# ---------------------------------------------------------------------------
async def _cs_create_callback(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    got_lock = await claim_ticket_create_lock(str(interaction.user.id), 'customerservice')
    if not await _claim_lock_or_bail_wrapper(interaction, 'customerservice', got_lock):
        return

    channel = await _create_ticket_channel(interaction, 'customerservice')

    if not await _finish_or_discard_duplicate(interaction, channel, got_lock):
        return

    await create_ticket({
        'guildId': str(interaction.guild_id),
        'channelId': str(channel.id),
        'category': 'customerservice',
        'creatorId': str(interaction.user.id),
    })
    await release_ticket_create_lock(str(interaction.user.id), 'customerservice')

    await channel.send(content=f'{interaction.user.mention} 💬 Please wait for an admin to answer your ticket.')
    await interaction.edit_original_response(content=f'✅ Ticket created: {channel.mention}', embed=None, view=None)


# ---------------------------------------------------------------------------
# /ticket send -- panel embed modal
# ---------------------------------------------------------------------------
class SendPanelModal(discord.ui.Modal, title='Ticket Panel'):
    panel_title = discord.ui.TextInput(label='Title', style=discord.TextStyle.short, required=True)
    panel_description = discord.ui.TextInput(label='Description', style=discord.TextStyle.paragraph, required=True)
    panel_color = discord.ui.TextInput(
        label='Color (hex, e.g. #5865F2)', placeholder='#5865F2', style=discord.TextStyle.short, required=False
    )

    def __init__(self, source_interaction: discord.Interaction, target_channel: discord.TextChannel):
        super().__init__()
        self.source_interaction = source_interaction
        self.target_channel = target_channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        color = COLOR_INFO
        color_raw = self.panel_color.value.strip()
        if color_raw:
            try:
                color = int(color_raw.replace('#', ''), 16)
            except ValueError:
                pass

        embed = discord.Embed(
            title=self.panel_title.value.strip(), description=self.panel_description.value.strip(), color=color
        )

        try:
            await self.target_channel.send(embed=embed, view=TicketPanelView())
        except discord.HTTPException as err:
            print(f'Failed to send ticket panel: {err}')
            return await interaction.followup.send(f"❌ Couldn't send the panel to {self.target_channel.mention}.", ephemeral=True)

        await interaction.followup.send(f'✅ Panel sent to {self.target_channel.mention}.', ephemeral=True)

        await log_command_activity(
            self.source_interaction, subcommand='send', success=True,
            fields={'discordUser': interaction.user, 'channel': self.target_channel},
        )


# ---------------------------------------------------------------------------
# /ticket done -- testimonial image modal
# ---------------------------------------------------------------------------
class DoneModal(discord.ui.Modal, title='Testimonial Image'):
    testi_image_url = discord.ui.TextInput(label='Image URL', style=discord.TextStyle.short, required=True)

    def __init__(self, source_interaction: discord.Interaction, channel_id: int, ticket: dict):
        super().__init__()
        self.source_interaction = source_interaction
        self.channel_id = channel_id
        self.ticket = ticket

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        image_url = self.testi_image_url.value.strip()

        testi_channel = await self._resolve_testi_channel(interaction)
        if not testi_channel:
            return await interaction.followup.send(
                '❌ Testimonial channel no longer exists. Run `/ticket settesti` again.', ephemeral=True
            )

        product_list = self._product_list_text()
        total_price = _format_idr(self.ticket['total']) if self.ticket.get('total') else 'N/A'
        ticket_number = await next_ticket_number(str(interaction.guild_id))

        testi_embed = discord.Embed(
            color=COLOR_SUCCESS,
            description=f'TERIMAKASIH SUDAH MEMBELI PRODUK: {product_list} DENGAN TOTAL HARGA: {total_price}',
        )
        testi_embed.set_image(url=image_url)
        testi_embed.set_footer(text=f'Testimonial number {ticket_number}')

        await testi_channel.send(embed=testi_embed)

        delivery_failures = await self._deliver_products(interaction)

        await close_ticket(str(self.channel_id), {'ticketNumber': ticket_number})

        await log_command_activity(
            self.source_interaction, subcommand='done', success=True,
            fields={'discordUser': interaction.user, 'ticketChannel': f'<#{self.channel_id}>', 'total': total_price},
        )

        note = f"\n⚠️ Couldn't fully deliver: {', '.join(delivery_failures)}. Check manually." if delivery_failures else ''
        await interaction.followup.send(f'✅ Ticket marked done, testimonial posted.{note}', ephemeral=True)

    async def _resolve_testi_channel(self, interaction: discord.Interaction):
        testi_channel_id = await get_testi_channel(str(interaction.guild_id))
        if not testi_channel_id:
            return None
        testi_channel = interaction.guild.get_channel(int(testi_channel_id))
        if testi_channel is None:
            try:
                testi_channel = await interaction.guild.fetch_channel(int(testi_channel_id))
            except discord.HTTPException:
                testi_channel = None
        return testi_channel

    def _product_list_text(self) -> str:
        products = self.ticket.get('products') or []
        if products:
            return ', '.join(p['name'] for p in products)
        return 'Service' if self.ticket.get('category') == 'service' else 'Customer Service'

    async def _deliver_products(self, interaction: discord.Interaction) -> list[str]:
        products = self.ticket.get('products') or []
        if not products:
            return []

        failures = []
        client = interaction.client
        creator = client.get_user(int(self.ticket['creatorId']))
        if creator is None:
            try:
                creator = await client.fetch_user(int(self.ticket['creatorId']))
            except discord.HTTPException:
                creator = None

        for item in products:
            try:
                await give_product_to_user(item['productId'], self.ticket['creatorId'])
            except Exception as err:  # noqa: BLE001
                print(f"Failed to grant product {item['productId']} to {self.ticket['creatorId']}: {err}")
                failures.append(item['name'])
                continue

            try:
                await auto_whitelist_product_for_user(item['productId'], self.ticket['creatorId'])
            except Exception as err:  # noqa: BLE001
                print(f"auto_whitelist_product_for_user failed for {item['productId']} / {self.ticket['creatorId']} (product still granted): {err}")

            product = await get_product(item['productId'])
            if not creator or not product or not product.get('fileLink'):
                continue

            try:
                await creator.send(**build_product_delivery_dm(product))
            except discord.HTTPException:
                failures.append(f"{item['name']} (DM failed)")
                continue

            try:
                await creator.send(
                    f"How was **{item['name']}**? Rate it with `/product rating` (X/10 + why) -- "
                    f"your review gets posted in the product's forum thread."
                )
            except discord.HTTPException:
                pass  # review nudge is best-effort, delivery already succeeded

        return failures


# ---------------------------------------------------------------------------
# Cog + slash commands
# ---------------------------------------------------------------------------
class TicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ticket_group = app_commands.Group(name='ticket', description='Ticket system')

    async def _guild_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message('This command only works inside a server.', ephemeral=True)
            return False
        return True

    @ticket_group.command(name='send', description='Send a ticket panel embed')
    @app_commands.describe(channel='Where to send the panel')
    async def send(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _deny_not_admin(interaction)

        perms = channel.permissions_for(interaction.guild.me)
        if not perms.send_messages or not perms.view_channel:
            return await interaction.response.send_message(
                f"❌ I can't send messages in {channel.mention}. Check my permissions there.", ephemeral=True
            )

        await interaction.response.send_modal(SendPanelModal(interaction, channel))

    @ticket_group.command(name='done', description='Mark the current ticket as done and post testimonial')
    async def done(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _deny_not_admin(interaction)

        channel_id = interaction.channel_id
        ticket = await get_ticket(str(channel_id))
        if not ticket:
            return await interaction.response.send_message('❌ This is not a ticket channel.', ephemeral=True)
        if ticket.get('status') == 'done':
            return await interaction.response.send_message('⚠️ This ticket is already marked done.', ephemeral=True)
        if ticket.get('status') == 'deleted':
            return await interaction.response.send_message('⚠️ This ticket has already been closed.', ephemeral=True)

        testi_channel_id = await get_testi_channel(str(interaction.guild_id))
        if not testi_channel_id:
            return await interaction.response.send_message(
                '❌ No testimonial channel set. Run `/ticket settesti` first.', ephemeral=True
            )

        await interaction.response.send_modal(DoneModal(interaction, channel_id, ticket))

    @ticket_group.command(name='settesti', description='Set the testimonial channel')
    @app_commands.describe(channel='Testimonial channel')
    async def settesti(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _deny_not_admin(interaction)

        await interaction.response.defer(ephemeral=True)
        await set_testi_channel(str(interaction.guild_id), str(channel.id))

        await log_command_activity(
            interaction, subcommand='settesti', success=True, fields={'discordUser': interaction.user, 'channel': channel}
        )

        await interaction.followup.send(f'✅ Testimonial channel set to {channel.mention}.', ephemeral=True)

    @ticket_group.command(name='createcategory', description='Create the Order/Service/Customer Service ticket categories')
    async def createcategory(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _deny_not_admin(interaction)

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        existing = await get_ticket_categories(str(interaction.guild_id)) or {}

        wanted = {'order': 'Order Tickets', 'service': 'Service Tickets', 'customerservice': 'Customer Service Tickets'}
        result = dict(existing)

        for key, name in wanted.items():
            still_exists = result.get(key) and guild.get_channel(int(result[key])) is not None
            if still_exists:
                continue
            created = await guild.create_category(name)
            result[key] = str(created.id)

        await set_ticket_categories(str(interaction.guild_id), result)

        await log_command_activity(interaction, subcommand='createcategory', success=True, fields={'discordUser': interaction.user})

        embed = discord.Embed(title='✅ Ticket Categories Ready', color=COLOR_SUCCESS)
        embed.add_field(name='Order', value=f"<#{result['order']}>", inline=True)
        embed.add_field(name='Service', value=f"<#{result['service']}>", inline=True)
        embed.add_field(name='Customer Service', value=f"<#{result['customerservice']}>", inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @ticket_group.command(name='close', description='Close and delete a ticket, DM the creator')
    @app_commands.describe(channel='Ticket channel to close (default: current channel)')
    async def close(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _deny_not_admin(interaction)

        await interaction.response.defer(ephemeral=True)

        target_channel = channel or interaction.channel

        ticket = await get_ticket(str(target_channel.id))
        if not ticket:
            return await interaction.followup.send(f'❌ {target_channel.mention} is not a ticket channel.', ephemeral=True)

        client = interaction.client
        creator = client.get_user(int(ticket['creatorId']))
        if creator is None:
            try:
                creator = await client.fetch_user(int(ticket['creatorId']))
            except discord.HTTPException:
                creator = None

        dm_sent = False
        if creator:
            try:
                await creator.send(f'🔒 Your ticket in **{interaction.guild.name}** has been closed.')
                dm_sent = True
            except discord.HTTPException:
                pass

        await mark_ticket_deleted(str(target_channel.id))

        try:
            await target_channel.delete(reason=f'Ticket closed by {interaction.user}')
        except discord.HTTPException as err:
            print(f'Failed to delete ticket channel: {err}')
            return await interaction.followup.send(
                f"❌ Couldn't delete {target_channel.mention}. Check my permissions there.", ephemeral=True
            )

        await log_command_activity(
            interaction, subcommand='close', success=True,
            fields={'discordUser': interaction.user, 'ticketChannel': f'<#{target_channel.id}>'},
        )

        note = '' if dm_sent else ' ⚠️ (Could not DM the creator -- DMs may be closed.)'
        await interaction.followup.send(f'✅ Ticket closed and deleted.{note}', ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
    # Register the persistent panel view so its buttons/selects keep working
    # after a bot restart.
    bot.add_view(TicketPanelView())
