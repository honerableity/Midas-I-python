"""/product command -- manage shop products.

Ported from commands/product.js.
"""
import re
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import log_command_activity
from utils.products import (
    create_or_sync_product_type_forum,
    delete_product,
    get_product,
    get_products_by_ids,
    give_product_to_user,
    link_existing_forum_to_type,
    list_product_types,
    list_products_by_type,
    revoke_product_from_user,
    save_product,
    user_owns_product,
    build_product_delivery_dm,
)
from utils.verification import get_verified_user

COMMAND_NAME = 'product'

LOG_SCHEMA = {
    'subcommands': {
        'create': {'label': 'Product — Created', 'fields': ['discordUser', 'productId', 'productName']},
        'createtype': {'label': 'Product — Type Created', 'fields': ['discordUser', 'typeName', 'forumChannel']},
        'linktype': {'label': 'Product — Type Linked', 'fields': ['discordUser', 'typeName', 'forumChannel']},
        'sendpost': {'label': 'Product — Post Sent', 'fields': ['discordUser', 'productId', 'forumChannel']},
        'edit': {'label': 'Product — Edited', 'fields': ['discordUser', 'productId', 'productName']},
        'view': {'label': 'Product — Browsed', 'fields': ['discordUser']},
        'delete': {'label': 'Product — Deleted', 'fields': ['discordUser', 'productId', 'productName']},
        'give': {'label': 'Product — Given', 'fields': ['discordUser', 'targetUser', 'productId', 'productName']},
        'revoke': {'label': 'Product — Revoked', 'fields': ['discordUser', 'targetUser', 'productId', 'productName']},
        'get': {'label': 'Product — File Link Requested', 'fields': ['discordUser', 'productId', 'productName']},
    },
}

STEP_TIMEOUT_S = 15 * 60
MAX_SELECT_OPTIONS = 25
_IMAGE_URL_RE = re.compile(r'\.(png|jpe?g|gif|webp)(\?.*)?$', re.IGNORECASE)
_URL_RE = re.compile(r'^https?://', re.IGNORECASE)


def _now_ms():
    return int(time.time() * 1000)


def _require_admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.administrator)


def _is_free_product(price) -> bool:
    normalized = str(price or '').strip().lower()
    return normalized in ('0', 'free')


async def _admin_denied(interaction: discord.Interaction):
    await interaction.response.send_message('You need **Administrator** permission to do that.', ephemeral=True)


# ---------------------------------------------------------------------------
# /product create -- modal(1/2) -> continue button -> modal(2/2) -> type select
# ---------------------------------------------------------------------------
class CreateModal1(discord.ui.Modal, title='New Product (1/2)'):
    product_name = discord.ui.TextInput(label='Nama produk', style=discord.TextStyle.short, required=True)
    product_description = discord.ui.TextInput(label='Deskripsi', style=discord.TextStyle.paragraph, required=True)
    product_price = discord.ui.TextInput(label='Harga', placeholder='cth: 25000 atau Rp25.000', style=discord.TextStyle.short, required=True)
    product_creator = discord.ui.TextInput(label='Kreator (kosongkan jika kamu sendiri)', style=discord.TextStyle.short, required=False)

    def __init__(self, source_interaction: discord.Interaction):
        super().__init__()
        self.source_interaction = source_interaction

    async def on_submit(self, interaction: discord.Interaction):
        name = self.product_name.value.strip()
        description = self.product_description.value.strip()
        price = self.product_price.value.strip()
        creator = self.product_creator.value.strip() or interaction.user.name

        view = discord.ui.View(timeout=STEP_TIMEOUT_S)
        continue_btn = discord.ui.Button(label='Lanjutkan (2/2)', style=discord.ButtonStyle.primary)

        async def _continue_cb(btn_interaction: discord.Interaction):
            modal2 = CreateModal2(
                source_interaction=self.source_interaction,
                name=name, description=description, price=price, creator=creator,
            )
            await btn_interaction.response.send_modal(modal2)

        continue_btn.callback = _continue_cb
        view.add_item(continue_btn)

        async def _on_timeout():
            try:
                await interaction.edit_original_response(content='Waktu habis. Jalankan `/product create` lagi.', view=None)
            except discord.HTTPException:
                pass

        view.on_timeout = _on_timeout

        await interaction.response.send_message(
            'Langkah 1 tersimpan. Klik tombol di bawah buat lanjut ke langkah 2.', view=view, ephemeral=True
        )


class CreateModal2(discord.ui.Modal, title='New Product (2/2)'):
    product_file_link = discord.ui.TextInput(label='Link file produk', placeholder='CDN Discord, catbox.moe, Drive, Mega.nz, dll', style=discord.TextStyle.short, required=True)
    product_review_media = discord.ui.TextInput(label='Video/Gambar Review Produk', placeholder='Link video atau gambar review', style=discord.TextStyle.short, required=True)
    product_tutorial_link = discord.ui.TextInput(label='Link Tutorial (opsional)', placeholder='Link tutorial cara pakai produk, boleh kosong', style=discord.TextStyle.short, required=False)

    def __init__(self, *, source_interaction: discord.Interaction, name, description, price, creator):
        super().__init__()
        self.source_interaction = source_interaction
        self.name = name
        self.description = description
        self.price = price
        self.creator = creator

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        file_link = self.product_file_link.value.strip()
        review_media = self.product_review_media.value.strip()
        tutorial_link = self.product_tutorial_link.value.strip()

        types = await list_product_types(str(interaction.guild_id))
        if not types:
            return await interaction.followup.send(
                'Belum ada jenis produk yang terdaftar. Minta admin jalankan `/product createtype` dulu, baru ulangi `/product create`.'
            )

        select = discord.ui.Select(
            placeholder='Pilih jenis produk',
            options=[discord.SelectOption(label=t['name'], value=t['id']) for t in types[:MAX_SELECT_OPTIONS]],
        )
        view = discord.ui.View(timeout=STEP_TIMEOUT_S)
        view.add_item(select)

        async def _on_timeout():
            try:
                await interaction.edit_original_response(content='Waktu habis memilih jenis. Jalankan `/product create` lagi.', view=None)
            except discord.HTTPException:
                pass

        view.on_timeout = _on_timeout

        async def _select_cb(select_interaction: discord.Interaction):
            selected_type_id = select.values[0]
            selected_type = next((t for t in types if t['id'] == selected_type_id), None)

            try:
                await select_interaction.response.defer_update() if hasattr(select_interaction.response, 'defer_update') else await select_interaction.response.defer()
            except Exception as err:  # noqa: BLE001
                print(f'select deferred failed (continuing anyway): {err}')

            product_id = str(uuid.uuid4())
            product_data = {
                'productId': product_id,
                'name': self.name,
                'description': self.description,
                'price': self.price,
                'fileLink': file_link,
                'tutorialLink': tutorial_link,
                'reviewMedia': review_media,
                'creator': self.creator,
                'type': selected_type['name'],
                'typeId': selected_type['id'],
                'typeForumId': selected_type.get('forumChannelId'),
                'createdBy': str(interaction.user.id),
                'guildId': str(interaction.guild_id),
                'createdAt': _now_ms(),
            }

            try:
                await save_product(product_id, product_data)
            except Exception as err:  # noqa: BLE001
                print(f'Failed to save product to Firestore: {err}')
                await log_command_activity(
                    self.source_interaction, subcommand='create', success=False,
                    fields={'discordUser': interaction.user, 'productName': self.name}, note='Firestore write failed.',
                )
                return await interaction.edit_original_response(content='Gagal menyimpan produk ke database. Coba lagi.', view=None)

            await log_command_activity(
                self.source_interaction, subcommand='create', success=True,
                fields={'discordUser': interaction.user, 'productId': product_id, 'productName': self.name},
            )

            embed = discord.Embed(title='Produk Berhasil Dibuat', color=0x57F287)
            embed.add_field(name='Nama Produk', value=self.name, inline=False)
            embed.add_field(name='ID Produk', value=f'`{product_id}`', inline=False)
            embed.add_field(name='Jenis', value=selected_type['name'], inline=True)
            embed.add_field(name='Harga', value=self.price, inline=True)
            embed.add_field(name='Kreator', value=self.creator, inline=True)

            await interaction.edit_original_response(content='Produk berhasil dibuat!', embed=embed, view=None)

        select.callback = _select_cb

        await interaction.followup.send('Terakhir, pilih jenis produk:', view=view)


# ---------------------------------------------------------------------------
# /product edit -- prefilled modal(1/2) -> continue -> modal(2/2) -> type select/keep
# ---------------------------------------------------------------------------
class EditModal1(discord.ui.Modal, title='Edit Product (1/2)'):
    def __init__(self, source_interaction: discord.Interaction, product: dict, product_id: str):
        super().__init__()
        self.source_interaction = source_interaction
        self.product = product
        self.product_id = product_id

        self.product_name = discord.ui.TextInput(label='Nama produk', style=discord.TextStyle.short, default=product['name'], required=True)
        self.product_description = discord.ui.TextInput(label='Deskripsi', style=discord.TextStyle.paragraph, default=product['description'], required=True)
        self.product_price = discord.ui.TextInput(label='Harga', placeholder='cth: 25000 atau Rp25.000', style=discord.TextStyle.short, default=product['price'], required=True)
        self.product_creator = discord.ui.TextInput(label='Kreator (kosongkan jika kamu sendiri)', style=discord.TextStyle.short, default=product.get('creator', ''), required=False)

        for item in (self.product_name, self.product_description, self.product_price, self.product_creator):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.product_name.value.strip()
        description = self.product_description.value.strip()
        price = self.product_price.value.strip()
        creator = self.product_creator.value.strip() or interaction.user.name

        view = discord.ui.View(timeout=STEP_TIMEOUT_S)
        continue_btn = discord.ui.Button(label='Lanjutkan (2/2)', style=discord.ButtonStyle.primary)

        async def _continue_cb(btn_interaction: discord.Interaction):
            modal2 = EditModal2(
                source_interaction=self.source_interaction, product=self.product, product_id=self.product_id,
                name=name, description=description, price=price, creator=creator,
            )
            await btn_interaction.response.send_modal(modal2)

        continue_btn.callback = _continue_cb
        view.add_item(continue_btn)

        async def _on_timeout():
            try:
                await interaction.edit_original_response(content=f'Waktu habis. Jalankan `/product edit {self.product_id}` lagi.', view=None)
            except discord.HTTPException:
                pass

        view.on_timeout = _on_timeout

        await interaction.response.send_message(
            'Langkah 1 tersimpan. Klik tombol di bawah buat lanjut ke langkah 2.', view=view, ephemeral=True
        )


class EditModal2(discord.ui.Modal, title='Edit Product (2/2)'):
    def __init__(self, *, source_interaction: discord.Interaction, product: dict, product_id: str, name, description, price, creator):
        super().__init__()
        self.source_interaction = source_interaction
        self.product = product
        self.product_id = product_id
        self.name = name
        self.description = description
        self.price = price
        self.creator = creator

        self.product_file_link = discord.ui.TextInput(label='Link file produk', placeholder='CDN Discord, catbox.moe, Drive, Mega.nz, dll', style=discord.TextStyle.short, default=product['fileLink'], required=True)
        self.product_review_media = discord.ui.TextInput(label='Video/Gambar Review Produk', placeholder='Link video atau gambar review', style=discord.TextStyle.short, default=product.get('reviewMedia', ''), required=True)
        self.product_tutorial_link = discord.ui.TextInput(label='Link Tutorial (opsional)', placeholder='Link tutorial cara pakai produk, boleh kosong', style=discord.TextStyle.short, default=product.get('tutorialLink', ''), required=False)

        for item in (self.product_file_link, self.product_review_media, self.product_tutorial_link):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        file_link = self.product_file_link.value.strip()
        review_media = self.product_review_media.value.strip()
        tutorial_link = self.product_tutorial_link.value.strip()

        types = await list_product_types(str(interaction.guild_id))
        if not types:
            return await interaction.followup.send('Belum ada jenis produk yang terdaftar. Minta admin jalankan `/product createtype` dulu.')

        select = discord.ui.Select(
            placeholder='Pilih jenis produk',
            options=[
                discord.SelectOption(label=t['name'], value=t['id'], default=(t['id'] == self.product.get('typeId')))
                for t in types[:MAX_SELECT_OPTIONS]
            ],
        )
        view = discord.ui.View(timeout=STEP_TIMEOUT_S)
        view.add_item(select)

        keep_btn = discord.ui.Button(label=f"Simpan dengan jenis \"{self.product['type']}\"", style=discord.ButtonStyle.secondary)
        view.add_item(keep_btn)

        async def _on_timeout():
            try:
                await interaction.edit_original_response(content=f'Waktu habis memilih jenis. Jalankan `/product edit {self.product_id}` lagi.', view=None)
            except discord.HTTPException:
                pass

        view.on_timeout = _on_timeout

        async def _finish(select_interaction: discord.Interaction, selected_type_id: str):
            selected_type = next((t for t in types if t['id'] == selected_type_id), None)

            try:
                await select_interaction.response.defer()
            except Exception as err:  # noqa: BLE001
                print(f'ack deferred failed (continuing anyway): {err}')

            updated_data = {
                **self.product,
                'productId': self.product_id,
                'name': self.name,
                'description': self.description,
                'price': self.price,
                'fileLink': file_link,
                'tutorialLink': tutorial_link,
                'reviewMedia': review_media,
                'creator': self.creator,
                'type': selected_type['name'],
                'typeId': selected_type['id'],
                'typeForumId': (
                    self.product.get('typeForumId')
                    if selected_type['id'] == self.product.get('typeId')
                    else selected_type.get('forumChannelId')
                ),
                'updatedAt': _now_ms(),
            }
            updated_data.pop('id', None)

            try:
                await save_product(self.product_id, updated_data)
            except Exception as err:  # noqa: BLE001
                print(f'Failed to save edited product to Firestore: {err}')
                await log_command_activity(
                    self.source_interaction, subcommand='edit', success=False,
                    fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': self.name},
                    note='Firestore write failed.',
                )
                return await interaction.edit_original_response(content='Gagal menyimpan perubahan produk ke database. Coba lagi.', view=None)

            await log_command_activity(
                self.source_interaction, subcommand='edit', success=True,
                fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': self.name},
            )

            embed = discord.Embed(title='Produk Berhasil Diedit', color=0x57F287)
            embed.add_field(name='Nama Produk', value=self.name, inline=False)
            embed.add_field(name='ID Produk', value=f'`{self.product_id}`', inline=False)
            embed.add_field(name='Jenis', value=selected_type['name'], inline=True)
            embed.add_field(name='Harga', value=self.price, inline=True)
            embed.add_field(name='Kreator', value=self.creator, inline=True)

            post_note = ' Jalankan `/product sendpost` untuk update post forum-nya juga.' if updated_data.get('forumThreadId') else ''

            await interaction.edit_original_response(content=f'Produk berhasil diedit!{post_note}', embed=embed, view=None)

        async def _select_cb(select_interaction: discord.Interaction):
            await _finish(select_interaction, select.values[0])

        async def _keep_cb(select_interaction: discord.Interaction):
            await _finish(select_interaction, self.product.get('typeId'))

        select.callback = _select_cb
        keep_btn.callback = _keep_cb

        await interaction.followup.send(
            'Terakhir, pilih jenis produk (atau klik tombol untuk tetap pakai jenis saat ini):', view=view
        )


# ---------------------------------------------------------------------------
# /product delete -- confirm-via-modal
# ---------------------------------------------------------------------------
class DeleteConfirmModal(discord.ui.Modal, title='Confirm Delete'):
    def __init__(self, source_interaction: discord.Interaction, product_id: str):
        super().__init__()
        self.source_interaction = source_interaction
        self.product_id = product_id
        self.confirm_uuid = discord.ui.TextInput(
            label='Ketik ulang UUID produk untuk konfirmasi', placeholder=product_id, style=discord.TextStyle.short, required=True
        )
        self.add_item(self.confirm_uuid)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        product = await get_product(self.product_id)
        if not product:
            await log_command_activity(
                self.source_interaction, subcommand='delete', success=False,
                fields={'discordUser': interaction.user, 'productId': self.product_id}, note='Product UUID not found.',
            )
            return await interaction.followup.send(f'Produk dengan ID `{self.product_id}` tidak ditemukan.')

        typed = self.confirm_uuid.value.strip()
        if typed != self.product_id:
            return await interaction.followup.send(
                f'UUID tidak cocok. Kamu ketik `{typed}`, seharusnya `{self.product_id}`. Jalankan `/product delete` lagi untuk mengulang.'
            )

        if product.get('forumThreadId') and product.get('typeForumId'):
            try:
                guild = interaction.guild
                forum_channel = guild.get_channel(int(product['typeForumId']))
                if forum_channel is None:
                    forum_channel = await guild.fetch_channel(int(product['typeForumId']))
                if forum_channel:
                    thread = guild.get_thread(int(product['forumThreadId']))
                    if thread is None:
                        thread = await guild.fetch_channel(int(product['forumThreadId']))
                    if thread:
                        await thread.delete()
            except Exception as err:  # noqa: BLE001
                print(f'Failed to delete forum thread during product delete (continuing anyway): {err}')

        try:
            await delete_product(self.product_id)
        except Exception as err:  # noqa: BLE001
            print(f'Failed to delete product from Firestore: {err}')
            await log_command_activity(
                self.source_interaction, subcommand='delete', success=False,
                fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': product['name']},
                note='Firestore delete failed.',
            )
            return await interaction.followup.send('Gagal menghapus produk dari database. Coba lagi.')

        await log_command_activity(
            self.source_interaction, subcommand='delete', success=True,
            fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': product['name']},
        )

        return await interaction.followup.send(f"Produk **{product['name']}** (`{self.product_id}`) berhasil dihapus.")


# ---------------------------------------------------------------------------
# /product view -- type/product paginator
# ---------------------------------------------------------------------------
class ProductViewView(discord.ui.View):
    def __init__(self, *, owner_id: int, guild_id: str, types: list[dict]):
        super().__init__(timeout=10 * 60)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.types = types
        self.type_index = 0
        self.product_index = 0
        self._products_cache: dict[str, list[dict]] = {}
        self.message: discord.Message | None = None
        self._build_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('Only the person who ran this command can use these buttons.', ephemeral=True)
            return False
        return True

    async def _get_products(self, type_index: int) -> list[dict]:
        t = self.types[type_index]
        if t['id'] not in self._products_cache:
            products = await list_products_by_type(self.guild_id, t['id'])
            products.sort(key=lambda p: p['name'])
            self._products_cache[t['id']] = products
        return self._products_cache[t['id']]

    async def build_embed(self) -> discord.Embed:
        t = self.types[self.type_index]
        products = await self._get_products(self.type_index)

        embed = discord.Embed(color=0x00B0F4)
        embed.set_footer(text=f'Jenis {self.type_index + 1}/{len(self.types)} — {t["name"]}')

        if not products:
            embed.title = t['name']
            embed.description = 'Belum ada produk di jenis ini.'
            return embed

        product = products[self.product_index]
        embed.title = product['name']
        embed.description = product['description']
        embed.add_field(name='Harga', value=product['price'], inline=True)
        embed.add_field(name='Jenis', value=product['type'], inline=True)
        embed.add_field(name='Kreator', value=product['creator'], inline=True)
        embed.add_field(name='ID Produk', value=f'`{product["productId"]}`', inline=False)

        review_media = product.get('reviewMedia') or ''
        if _IMAGE_URL_RE.search(review_media):
            embed.set_image(url=review_media)
        elif review_media:
            embed.add_field(name='Video/Gambar Review', value=review_media, inline=False)

        embed.set_footer(text=f'Jenis {self.type_index + 1}/{len(self.types)} — {t["name"]} · Produk {self.product_index + 1}/{len(products)}')
        return embed

    def _build_components(self, disabled: bool = False):
        self.clear_items()
        # Product count for current view is computed synchronously from cache
        # (populated already once build_embed has run at least once); use a
        # conservative default of "more than one" until known to avoid
        # disabling prematurely.
        products = self._products_cache.get(self.types[self.type_index]['id'], [])

        type_prev = discord.ui.Button(label='◀◀ Jenis', style=discord.ButtonStyle.secondary, disabled=disabled or len(self.types) <= 1)
        product_prev = discord.ui.Button(label='◀ Produk', style=discord.ButtonStyle.secondary, disabled=disabled or len(products) <= 1 or self.product_index == 0)
        product_next = discord.ui.Button(label='Produk ▶', style=discord.ButtonStyle.secondary, disabled=disabled or len(products) <= 1 or self.product_index >= max(len(products) - 1, 0))
        type_next = discord.ui.Button(label='Jenis ▶▶', style=discord.ButtonStyle.secondary, disabled=disabled or len(self.types) <= 1)

        async def _type_prev_cb(interaction: discord.Interaction):
            self.type_index = (self.type_index - 1) % len(self.types)
            self.product_index = 0
            await self._get_products(self.type_index)
            self._build_components()
            await interaction.response.edit_message(embed=await self.build_embed(), view=self)

        async def _type_next_cb(interaction: discord.Interaction):
            self.type_index = (self.type_index + 1) % len(self.types)
            self.product_index = 0
            await self._get_products(self.type_index)
            self._build_components()
            await interaction.response.edit_message(embed=await self.build_embed(), view=self)

        async def _product_prev_cb(interaction: discord.Interaction):
            self.product_index = max(0, self.product_index - 1)
            self._build_components()
            await interaction.response.edit_message(embed=await self.build_embed(), view=self)

        async def _product_next_cb(interaction: discord.Interaction):
            products = await self._get_products(self.type_index)
            self.product_index = min(len(products) - 1, self.product_index + 1)
            self._build_components()
            await interaction.response.edit_message(embed=await self.build_embed(), view=self)

        type_prev.callback = _type_prev_cb
        product_prev.callback = _product_prev_cb
        product_next.callback = _product_next_cb
        type_next.callback = _type_next_cb

        for btn in (type_prev, product_prev, product_next, type_next):
            self.add_item(btn)

    async def on_timeout(self):
        self._build_components(disabled=True)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# /product get -- autocomplete
# ---------------------------------------------------------------------------
async def _autocomplete_get_product_uuid(interaction: discord.Interaction, current: str):
    focused = (current or '').lower()

    verified_record = await get_verified_user(str(interaction.user.id))
    owned_ids = (verified_record or {}).get('ownedProducts')
    if not owned_ids:
        return []

    owned = await get_products_by_ids(owned_ids)
    filtered = [p for p in owned if focused in (p.get('name') or '').lower()][:25]
    return [app_commands.Choice(name=p['name'][:100], value=p['id']) for p in filtered]


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class ProductCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    product_group = app_commands.Group(name='product', description='Manage shop products')

    async def _guild_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message('This command only works inside a server.', ephemeral=True)
            return False
        return True

    @product_group.command(name='create', description='Create a new product listing')
    async def create(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)
        await interaction.response.send_modal(CreateModal1(source_interaction=interaction))

    @product_group.command(name='createtype', description='Create (or re-sync) a product type and its dedicated forum channel')
    @app_commands.describe(nama='Nama jenis produk')
    async def createtype(self, interaction: discord.Interaction, nama: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        type_name = nama.strip()
        if not type_name:
            return await interaction.followup.send('Nama jenis tidak boleh kosong.')

        try:
            result = await create_or_sync_product_type_forum(interaction.guild, str(interaction.guild_id), type_name)
        except Exception as err:  # noqa: BLE001
            print(f'create_or_sync_product_type_forum failed: {err}')
            await log_command_activity(
                interaction, subcommand='createtype', success=False,
                fields={'discordUser': interaction.user, 'typeName': type_name}, note='Forum channel creation/sync failed.',
            )
            return await interaction.followup.send('Bot error saat membuat/menyinkronkan forum jenis produk. Cek permission Manage Channels bot.')

        await log_command_activity(
            interaction, subcommand='createtype', success=True,
            fields={'discordUser': interaction.user, 'typeName': type_name, 'forumChannel': result['forumChannel']},
        )

        verb = 'dibuat' if result['created'] else 'disinkronkan ulang'
        await interaction.followup.send(f'Jenis produk **{type_name}** {verb}. Forum: {result["forumChannel"].mention}')

    @product_group.command(name='linktype', description='Link a product type to an already-existing forum channel')
    @app_commands.describe(nama='Nama jenis produk', channel='Existing forum channel to link')
    async def linktype(self, interaction: discord.Interaction, nama: str, channel: discord.ForumChannel):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        type_name = nama.strip()
        if not type_name:
            return await interaction.followup.send('Nama jenis tidak boleh kosong.')

        try:
            result = await link_existing_forum_to_type(interaction.guild, str(interaction.guild_id), type_name, channel)
        except Exception as err:  # noqa: BLE001
            print(f'link_existing_forum_to_type failed: {err}')
            await log_command_activity(
                interaction, subcommand='linktype', success=False,
                fields={'discordUser': interaction.user, 'typeName': type_name}, note='Linking existing forum channel failed.',
            )
            return await interaction.followup.send('Bot error saat menghubungkan jenis produk ke channel. Cek permission Manage Channels bot di channel tersebut.')

        await log_command_activity(
            interaction, subcommand='linktype', success=True,
            fields={'discordUser': interaction.user, 'typeName': type_name, 'forumChannel': result['forumChannel']},
        )

        note = (
            f'Jenis produk **{type_name}** sekarang terhubung ke {result["forumChannel"].mention}. Forum lama (kalau berbeda) tidak dihapus.'
            if result['wasExistingType']
            else f'Jenis produk **{type_name}** dibuat dan dihubungkan ke {result["forumChannel"].mention}.'
        )
        await interaction.followup.send(note)

    @product_group.command(name='sendpost', description="Post a product to its type's forum channel")
    @app_commands.describe(product_uuid='ID produk (UUID)')
    async def sendpost(self, interaction: discord.Interaction, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        product_id = product_uuid.strip()
        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='sendpost', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.followup.send(
                f'Produk dengan ID `{product_id}` tidak ditemukan. Kalau produk ini baru dihapus, post forum-nya seharusnya sudah ikut terhapus lewat `/product delete`.'
            )

        if not product.get('typeForumId'):
            await log_command_activity(
                interaction, subcommand='sendpost', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product has no associated forum (type forum missing).',
            )
            return await interaction.followup.send(f"Produk ini belum punya forum jenis yang valid. Jalankan `/product createtype` untuk jenis **{product['type']}** dulu.")

        guild = interaction.guild
        forum_channel = guild.get_channel(int(product['typeForumId']))
        if forum_channel is None:
            try:
                forum_channel = await guild.fetch_channel(int(product['typeForumId']))
            except discord.HTTPException:
                forum_channel = None

        if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
            await log_command_activity(
                interaction, subcommand='sendpost', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Forum channel missing or no longer a forum channel.',
            )
            return await interaction.followup.send('Forum untuk jenis produk ini sudah tidak ada. Jalankan `/product createtype` lagi untuk membuatnya ulang.')

        is_free = _is_free_product(product['price'])

        embed = discord.Embed(title=product['name'], color=0x00B0F4)
        embed.add_field(name='Harga', value='GRATIS' if is_free else product['price'], inline=True)
        embed.add_field(name='Jenis', value=product['type'], inline=True)
        embed.add_field(name='Kreator', value=product['creator'], inline=True)

        if not is_free:
            embed.add_field(name='Link File', value=product['fileLink'], inline=False)

        review_media = product.get('reviewMedia') or ''
        if _IMAGE_URL_RE.search(review_media):
            embed.set_image(url=review_media)
        else:
            embed.add_field(name='Video/Gambar Review', value=review_media, inline=False)

        view = None
        if is_free:
            if _URL_RE.match(product.get('fileLink', '') or ''):
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label='Download', style=discord.ButtonStyle.link, url=product['fileLink']))
            else:
                embed.add_field(name='Link File', value=product['fileLink'], inline=False)

        post_content = f"{product['description']}\n\nTutorial: {product['tutorialLink']}" if (is_free and product.get('tutorialLink')) else product['description']

        existing_thread = None
        if product.get('forumThreadId'):
            existing_thread = guild.get_thread(int(product['forumThreadId']))
            if existing_thread is None:
                try:
                    existing_thread = await guild.fetch_channel(int(product['forumThreadId']))
                except discord.HTTPException:
                    existing_thread = None

        thread = None
        was_update = False

        if existing_thread:
            try:
                if existing_thread.name != product['name']:
                    await existing_thread.edit(name=product['name'])
                starter_message = None
                try:
                    starter_message = existing_thread.starter_message or await existing_thread.fetch_message(existing_thread.id)
                except discord.HTTPException:
                    starter_message = None
                if not starter_message:
                    raise RuntimeError('Starter message missing, cannot edit in place.')
                await starter_message.edit(content=post_content, embed=embed, view=view)
                thread = existing_thread
                was_update = True
            except Exception as err:  # noqa: BLE001
                print(f'Failed to edit existing forum post in place, falling back to new thread: {err}')
                existing_thread = None

        if not existing_thread:
            try:
                thread_with_message = await forum_channel.create_thread(
                    name=product['name'], content=post_content, embed=embed, view=view,
                )
                thread = thread_with_message.thread
            except Exception as err:  # noqa: BLE001
                print(f'Forum post creation failed: {err}')
                await log_command_activity(
                    interaction, subcommand='sendpost', success=False,
                    fields={'discordUser': interaction.user, 'productId': product_id}, note='Bot error while creating forum post.',
                )
                return await interaction.followup.send('Bot error saat membuat post di forum. Cek permission bot di channel forum tersebut.')

        await log_command_activity(
            interaction, subcommand='sendpost', success=True,
            fields={'discordUser': interaction.user, 'productId': product_id, 'forumChannel': forum_channel},
            note='Updated existing post in place.' if was_update else 'Created new post.',
        )

        if not was_update:
            try:
                await save_product(product_id, {**product, 'forumThreadId': str(thread.id)})
            except Exception as err:  # noqa: BLE001
                print(f'Failed to save forumThreadId onto product (post itself succeeded): {err}')

        verb = 'diperbarui' if was_update else 'diposting'
        await interaction.followup.send(f'Produk **{product["name"]}** berhasil {verb}: {thread.mention}')

    @product_group.command(name='edit', description='Edit an existing product listing')
    @app_commands.describe(product_uuid='ID produk (UUID)')
    async def edit(self, interaction: discord.Interaction, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        product_id = product_uuid.strip()
        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='edit', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.response.send_message(f'Produk dengan ID `{product_id}` tidak ditemukan.', ephemeral=True)

        await interaction.response.send_modal(EditModal1(interaction, product, product_id))

    @product_group.command(name='view', description='Browse all products by type')
    async def view(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return

        await interaction.response.defer()

        types = await list_product_types(str(interaction.guild_id))
        types.sort(key=lambda t: t['name'])

        if not types:
            return await interaction.followup.send('Belum ada jenis produk yang terdaftar.')

        await log_command_activity(interaction, subcommand='view', success=True, fields={'discordUser': interaction.user})

        view = ProductViewView(owner_id=interaction.user.id, guild_id=str(interaction.guild_id), types=types)
        embed = await view.build_embed()
        view._build_components()
        message = await interaction.followup.send(embed=embed, view=view, wait=True)
        view.message = message

    @product_group.command(name='delete', description='Delete a product')
    @app_commands.describe(product_uuid='ID produk (UUID)')
    async def delete(self, interaction: discord.Interaction, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        product_id = product_uuid.strip()
        await interaction.response.send_modal(DeleteConfirmModal(interaction, product_id))

    @product_group.command(name='give', description='Give a product to a verified user')
    @app_commands.describe(user='Target user', product_uuid='ID produk (UUID)')
    async def give(self, interaction: discord.Interaction, user: discord.User, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        product_id = product_uuid.strip()

        verified_record = await get_verified_user(str(user.id))
        if not verified_record:
            await log_command_activity(
                interaction, subcommand='give', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id}, note='Target user is not verified.',
            )
            return await interaction.followup.send(f'{user.mention} belum verifikasi. Suruh mereka jalankan `/verify start` dulu.')

        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='give', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.followup.send(f'Produk dengan ID `{product_id}` tidak ditemukan.')

        if user_owns_product(product, str(user.id)):
            await log_command_activity(
                interaction, subcommand='give', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
                note='Target user already owns this product.',
            )
            return await interaction.followup.send(f"{user.mention} sudah punya produk **{product['name']}**.")

        try:
            await give_product_to_user(product_id, str(user.id))
        except Exception as err:  # noqa: BLE001
            print(f'Failed to give product (Firestore write failed): {err}')
            await log_command_activity(
                interaction, subcommand='give', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
                note='Firestore write failed.',
            )
            return await interaction.followup.send('Gagal memberikan produk ke database. Coba lagi.')

        await log_command_activity(
            interaction, subcommand='give', success=True,
            fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
        )

        await interaction.followup.send(f"Produk **{product['name']}** berhasil diberikan ke {user.mention}.")

    @product_group.command(name='revoke', description='Revoke a product from a user')
    @app_commands.describe(user='Target user', product_uuid='ID produk (UUID)')
    async def revoke(self, interaction: discord.Interaction, user: discord.User, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        product_id = product_uuid.strip()

        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='revoke', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.followup.send(f'Produk dengan ID `{product_id}` tidak ditemukan.')

        if not user_owns_product(product, str(user.id)):
            await log_command_activity(
                interaction, subcommand='revoke', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
                note='Target user does not own this product.',
            )
            return await interaction.followup.send(f"{user.mention} belum punya produk **{product['name']}**.")

        try:
            await revoke_product_from_user(product_id, str(user.id))
        except Exception as err:  # noqa: BLE001
            print(f'Failed to revoke product (Firestore write failed): {err}')
            await log_command_activity(
                interaction, subcommand='revoke', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
                note='Firestore write failed.',
            )
            return await interaction.followup.send('Gagal mencabut produk dari database. Coba lagi.')

        await log_command_activity(
            interaction, subcommand='revoke', success=True,
            fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
        )

        await interaction.followup.send(f"Produk **{product['name']}** berhasil dicabut dari {user.mention}.")

    @product_group.command(name='get', description='Get the file link of a product you own, sent to your DM')
    @app_commands.describe(product_uuid='ID produk (UUID)')
    @app_commands.autocomplete(product_uuid=_autocomplete_get_product_uuid)
    async def get(self, interaction: discord.Interaction, product_uuid: str):
        await interaction.response.defer(ephemeral=True)

        product_id = product_uuid.strip()

        verified_record = await get_verified_user(str(interaction.user.id))
        if not verified_record:
            await log_command_activity(
                interaction, subcommand='get', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Requesting user is not verified.',
            )
            return await interaction.followup.send('You are required to verified to use this command!')

        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='get', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.followup.send(f'Produk dengan ID `{product_id}` tidak ditemukan.')

        if not user_owns_product(product, str(interaction.user.id)):
            await log_command_activity(
                interaction, subcommand='get', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id, 'productName': product['name']},
                note='Requesting user does not own this product.',
            )
            return await interaction.followup.send('You didnt owned the product!')

        try:
            await interaction.user.send(**build_product_delivery_dm(product))
        except discord.HTTPException:
            await log_command_activity(
                interaction, subcommand='get', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id, 'productName': product['name']},
                note='Could not DM the user (DMs likely closed).',
            )
            return await interaction.followup.send('Could not DM you the file link. Please enable DMs from server members and try again.')

        await log_command_activity(
            interaction, subcommand='get', success=True,
            fields={'discordUser': interaction.user, 'productId': product_id, 'productName': product['name']},
        )

        await interaction.followup.send('Sent! Check your DMs 📬')


async def setup(bot: commands.Bot):
    await bot.add_cog(ProductCog(bot))
