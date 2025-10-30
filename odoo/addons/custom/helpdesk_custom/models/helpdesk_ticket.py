# -*- coding: utf-8 -*-

from odoo import models, fields, api, _
from odoo.tools import html2plaintext  # pyright: ignore[reportMissingImports]
# pyright: ignore[reportMissingImports]
from odoo.tools.misc import format_datetime
from odoo.fields import Domain
# pyright: ignore[reportMissingImports]
from odoo.exceptions import ValidationError


class HelpdeskTicket(models.Model):
    _inherit = 'helpdesk.ticket'

    # Let Odoo enforce cross-company consistency where possible
    _check_company_auto = True

    # -------------------------------------------------------------------------
    # Fields
    # -------------------------------------------------------------------------

    category_id = fields.Many2one(
        'helpdesk.category',
        string='Category',
        required=True,
        tracking=True
    )

    allowed_user_ids = fields.Many2many(
        'res.users',
        string='Allowed Users',
        compute='_compute_allowed_user_ids',
        store=False,
        # compute_sudo=True,  # uncomment if you want full visibility regardless of the editor's access to users
        help='Team members allowed to be assigned to this ticket.'
    )

    branch_id = fields.Many2one(
        'res.company',
        string='Branch',
        help='Select the branch (child company) for this ticket.'
    )

    branch_display_name = fields.Char(
        string='Branch',
        compute='_compute_branch_display_name',
        inverse='_inverse_branch_display_name'
    )

    media_attachment_ids = fields.Many2many(
        'ir.attachment',
        'helpdesk_ticket_media_rel',
        'ticket_id',
        'attachment_id',
        string='Media Files',
        domain=['|', ('mimetype', 'ilike', 'image/%'),
                ('mimetype', 'ilike', 'video/%')],
        help='Attach images or videos to this ticket.'
    )

    media_preview_html = fields.Html(
        string='Media Preview',
        compute='_compute_media_preview_html',
        sanitize=False,
    )

    preview_compact = fields.Char(
        string='Preview Icon',
        compute='_compute_preview_compact',
    )

    preview_uid = fields.Char(
        string='Preview UID',
        compute='_compute_preview_uid',
        help='Internal record id for tooltip resolution',
    )

    vendor_id = fields.Many2one(
        'res.partner',
        string='Vendor',
        domain=[('is_company', '=', True)],
        help='Select the vendor relevant to this ticket.'
    )

    # Hidden bump to force SLA recomputation via _sla_reset_trigger
    sla_recompute_bump = fields.Integer(string='SLA Recompute Bump', default=0)

    sla_opt_out = fields.Boolean(
        string='Ignore SLA',
        default=False,
        help='If enabled, this ticket ignores SLA policies and no SLA statuses will be created.'
    )

    media_counts = fields.Char(
        string='Media Counts', compute='_compute_media_counts')

    # -------------------------------------------------------------------------
    # Onchanges / Computes
    # -------------------------------------------------------------------------

    @api.onchange('category_id')
    def _onchange_category_id_set_team_and_user(self):
        for ticket in self:
            if not ticket.category_id:
                continue

            # 1) Team: category.team OR nearest parent.team OR a default team in the ticket's company
            new_team = ticket.category_id.team_id
            if new_team:
                ticket.team_id = new_team
            else:
                if not ticket.team_id:
                    parent = ticket.category_id.parent_id
                    while parent and not parent.team_id:
                        parent = parent.parent_id
                    if parent and parent.team_id:
                        ticket.team_id = parent.team_id
                    elif not ticket.team_id:
                        company = ticket.company_id or self.env.company
                        default_team = self.env['helpdesk.team'].search(
                            [('company_id', '=', company.id)], limit=1)
                        if default_team:
                            ticket.team_id = default_team

            # 2) Assignee: if team assigns by category, set to category.user if member, else clear
            target_team = ticket.team_id
            if target_team and getattr(target_team, 'assign_method', False) == 'category':
                category_user = ticket.category_id.user_id
                ticket.user_id = category_user if (
                    category_user and category_user in target_team.member_ids) else False

            # 3) Default SLA selection on first category selection: prefill a single SLA if none chosen yet
            if not ticket.sla_opt_out and not ticket.sla_ids:
                try:
                    ancestors = self.env['helpdesk.category'].search([
                        ('id', 'parent_of', ticket.category_id.id)
                    ]).ids
                    default_slas = self.env['helpdesk.sla'].search([
                        ('category_ids', 'in', ancestors)
                    ])
                    if default_slas:
                        ticket.sla_ids = [(6, 0, [default_slas[:1].id])]
                except Exception:
                    # Best-effort only; do not block form onchange
                    pass

        # 4) Assignee domain: limit to team members
        if len(self) == 1:
            allowed_ids = self.team_id.member_ids.ids if self.team_id else []
            return {'domain': {'user_id': [('id', 'in', allowed_ids)]}}

    @api.onchange('sla_ids')
    def _onchange_sla_ids_limit_one(self):
        for ticket in self:
            if ticket.sla_ids and len(ticket.sla_ids) > 1:
                # Keep only the first SLA in the set
                ticket.sla_ids = [(6, 0, [ticket.sla_ids[:1].id])]

    @api.onchange('team_id')
    def _onchange_team_id_set_user_domain(self):
        if len(self) == 1:
            allowed_ids = self.team_id.member_ids.ids if self.team_id else []
            return {'domain': {'user_id': [('id', 'in', allowed_ids)]}}

    @api.onchange('company_id')
    def _onchange_company_set_branch_domain(self):
        if len(self) != 1:
            return
        allowed_company_ids = self.env.companies.ids  # user's allowed companies
        domain = [('id', 'in', allowed_company_ids)]
        if self.company_id:
            # Include main company along with its children
            allowed = self.env['res.company'].search([
                ('id', 'in', allowed_company_ids),
                '|', ('id', 'child_of', self.company_id.id), ('id',
                                                              '=', self.company_id.id),
            ]).ids
            domain = [('id', 'in', allowed or [])]
            if self.branch_id and self.branch_id.id not in (allowed or []):
                self.branch_id = False
        return {'domain': {'branch_id': domain}}

    @api.onchange('category_id', 'team_id', 'priority', 'stage_id', 'tag_ids', 'sla_opt_out')
    def _onchange_sla_ids_domain(self):
        if len(self) != 1:
            return
        ticket = self
        # When opting out, prevent selecting any SLA
        if ticket.sla_opt_out:
            return {'domain': {'sla_ids': [('id', '=', 0)]}}

        domain = []
        # Team matching (or global)
        if ticket.team_id:
            domain += ['|', ('team_id', '=', False),
                       ('team_id', '=', ticket.team_id.id)]
        else:
            domain += [('team_id', '=', False)]
        # Priority match (or all priorities)
        domain += ['|', ('priority_all', '=', True),
                   ('priority', '=', ticket.priority)]
        # Stage sequence: SLA target stage sequence must be >= current
        if ticket.stage_id:
            domain += [('stage_id.sequence', '>=', ticket.stage_id.sequence)]
        # Category: SLA without category or ancestor of ticket category
        if ticket.category_id:
            ancestors = self.env['helpdesk.category'].search([
                ('id', 'parent_of', ticket.category_id.id)
            ]).ids
            domain += ['|', ('category_ids', '=', False),
                       ('category_ids', 'in', ancestors)]
        else:
            domain += [('category_ids', '=', False)]
        # Tags: SLA with no tags or overlapping any ticket tag
        if ticket.tag_ids:
            domain += ['|', ('tag_ids', '=', False),
                       ('tag_ids', 'in', ticket.tag_ids.ids)]
        else:
            domain += [('tag_ids', '=', False)]
        return {'domain': {'sla_ids': domain}}

    @api.depends('team_id', 'team_id.member_ids')
    def _compute_allowed_user_ids(self):
        Users = self.env['res.users']
        for ticket in self:
            ticket.allowed_user_ids = ticket.team_id.member_ids if ticket.team_id else Users.browse([
            ])

    @api.depends('media_attachment_ids', 'media_attachment_ids.mimetype')
    def _compute_media_preview_html(self):
        Attachment = self.env['ir.attachment']  # respect ACLs
        for ticket in self:
            images = [att for att in ticket.media_attachment_ids if (
                att.mimetype or '').startswith('image/')]
            if not images and ticket.id:
                images = Attachment.search([
                    ('res_model', '=', 'helpdesk.ticket'),
                    ('res_id', '=', ticket.id),
                    ('mimetype', 'ilike', 'image/%')
                ], order='id desc', limit=4)
            if not images:
                ticket.media_preview_html = False
                continue

            parts = []
            max_icons = 3
            for att in images[:max_icons]:
                url = f"/web/content/{att.id}?download=false"
                parts.append(
                    f'<span class="o_ticket_media_cell" style="margin-right:6px;" '
                    f'data-media-url="{url}" title="Preview">🖼️</span>'
                )
            extra = max(0, len(images) - max_icons)
            if extra:
                parts.append(f'<span class="text-muted">+{extra}</span>')
            ticket.media_preview_html = ''.join(parts)

    @api.depends(
        'name', 'ticket_ref', 'category_id', 'partner_id', 'user_id', 'stage_id',
        'priority', 'create_date', 'sla_deadline', 'branch_id', 'description',
        'message_ids', 'message_ids.body'
    )
    def _compute_preview_compact(self):
        for ticket in self:
            ticket.preview_compact = 'ℹ️'

    @api.depends('branch_id', 'branch_id.company_registry', 'branch_id.name')
    def _compute_branch_display_name(self):
        for ticket in self:
            if ticket.branch_id:
                registry = (ticket.branch_id.company_registry or '').strip()
                name = ticket.branch_id.name or ''
                ticket.branch_display_name = f"{
                    registry}-{name}" if registry else name
            else:
                ticket.branch_display_name = False

    def _inverse_branch_display_name(self):
        for ticket in self:
            if not ticket.branch_id:
                # Without a selected branch company, editing the display has no target
                continue
            value = (ticket.branch_display_name or '').strip()
            if not value:
                # Do not erase company fields when cleared from UI
                continue
            if '-' in value:
                registry_part, name_part = value.split('-', 1)
                registry_part = (registry_part or '').strip()
                name_part = (name_part or '').strip()
            else:
                registry_part, name_part = '', value
            write_vals = {}
            if registry_part != (ticket.branch_id.company_registry or ''):
                write_vals['company_registry'] = registry_part
            if name_part and name_part != (ticket.branch_id.name or ''):
                write_vals['name'] = name_part
            if write_vals:
                ticket.branch_id.write(write_vals)

    def _compute_preview_uid(self):
        for ticket in self:
            ticket.preview_uid = str(ticket.id or '')

    @api.depends('media_attachment_ids', 'media_attachment_ids.mimetype')
    def _compute_media_counts(self):
        Attachment = self.env['ir.attachment']  # respect ACLs
        for ticket in self:
            atts = [a for a in ticket.media_attachment_ids if (
                a.mimetype or '').startswith(('image/', 'video/'))]
            if not atts and ticket.id:
                atts = Attachment.search([
                    ('res_model', '=', 'helpdesk.ticket'),
                    ('res_id', '=', ticket.id),
                    '|', ('mimetype', 'ilike',
                          'image/%'), ('mimetype', 'ilike', 'video/%')
                ], order='id desc', limit=50)
            img = sum(1 for a in atts if (
                a.mimetype or '').startswith('image/'))
            vid = sum(1 for a in atts if (
                a.mimetype or '').startswith('video/'))
            parts = []
            if img:
                parts.append(f"🖼️ {img}")
            if vid:
                parts.append(f"🎞️ {vid}")
            ticket.media_counts = ' '.join(parts)

    # -------------------------------------------------------------------------
    # Create / Write
    # -------------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        Team = self.env['helpdesk.team']
        Category = self.env['helpdesk.category']
        new_vals_list = []

        for vals in vals_list:
            category_id = vals.get('category_id')
            if category_id:
                category = Category.browse(category_id)

                # 1) Sync team from category (or closest parent) or fall back to any team in company
                if category.team_id:
                    vals['team_id'] = category.team_id.id
                elif not vals.get('team_id'):
                    parent = category.parent_id
                    while parent and not parent.team_id:
                        parent = parent.parent_id
                    if parent and parent.team_id:
                        vals['team_id'] = parent.team_id.id
                    elif not vals.get('team_id'):
                        company_id = vals.get(
                            'company_id') or self.env.company.id
                        default_team = Team.search(
                            [('company_id', '=', company_id)], limit=1)
                        if default_team:
                            vals['team_id'] = default_team.id

                # 2) Conditionally sync assignee based on team setting
                team = category.team_id or (
                    vals.get('team_id') and Team.browse(vals['team_id']))
                if team and getattr(team, 'assign_method', False) == 'category':
                    # Respect manual assignee if provided; otherwise, assign from category if valid
                    if not vals.get('user_id'):
                        vals['user_id'] = (
                            category.user_id.id
                            if (
                                category.user_id and category.user_id in team.member_ids
                            )
                            else False
                        )

            # Ensure at most one SLA is set
            if vals.get('sla_ids'):
                try:
                    cmds = vals['sla_ids'] or []
                    # Normalize many2many commands to final ids
                    ids = set()
                    for cmd in cmds:
                        if not isinstance(cmd, (list, tuple)):
                            continue
                        if not cmd:
                            continue
                        op = cmd[0]
                        if op == 6:
                            ids = set(cmd[2] or [])
                        elif op == 4:
                            ids.add(cmd[1])
                        elif op == 3:
                            ids.discard(cmd[1])
                        elif op == 5:
                            ids.clear()
                    if ids:
                        vals['sla_ids'] = [(6, 0, [next(iter(ids))])]
                except Exception:
                    pass

            new_vals_list.append(vals)

        return super().create(new_vals_list)

    def write(self, vals):
        if 'category_id' in vals:
            Category = self.env['helpdesk.category']
            category = Category.browse(vals.get('category_id')) if vals.get(
                'category_id') else False

            if category:
                # 1) Sync team from category (or closest parent)
                if category.team_id:
                    vals['team_id'] = category.team_id.id
                elif 'team_id' not in vals:
                    parent = category.parent_id
                    while parent and not parent.team_id:
                        parent = parent.parent_id
                    if parent and parent.team_id:
                        vals['team_id'] = parent.team_id.id

                # 2) Conditionally sync assignee based on target team setting
                target_team = category.team_id
                if 'team_id' in vals and vals['team_id']:
                    target_team = self.env['helpdesk.team'].browse(
                        vals['team_id'])
                if target_team and getattr(target_team, 'assign_method', False) == 'category':
                    # Only auto-assign when no manual assignee is being set and no current assignee
                    if 'user_id' not in vals and len(self) == 1 and not self.user_id:
                        vals['user_id'] = (
                            category.user_id.id
                            if (
                                category.user_id and category.user_id in target_team.member_ids
                            )
                            else False
                        )
        res = super().write(vals)
        # If multiple SLAs slipped in via other flows, compress to one (keep first)
        try:
            for ticket in self:
                if ticket.sla_ids and len(ticket.sla_ids) > 1:
                    target = ticket.sla_ids[:1]
                    super(HelpdeskTicket, ticket).write(
                        {'sla_ids': [(6, 0, [target.id])]})
        except Exception:
            pass
        # After core SLA apply (keep_reached=True), remove statuses whose SLA no longer matches
        try:
            self._cleanup_obsolete_sla_statuses_after_change(vals)
        except Exception:
            pass
        return res

    # -------------------------------------------------------------------------
    # Constraints
    # -------------------------------------------------------------------------

    @api.constrains('team_id', 'user_id')
    def _check_assignee_is_team_member(self):
        for ticket in self:
            if ticket.team_id and ticket.user_id and ticket.user_id not in ticket.team_id.member_ids:
                raise ValidationError(
                    _('Assign To must be a member of the selected Helpdesk Team.'))

    @api.constrains('company_id', 'branch_id')
    def _check_branch_is_child_of_company(self):
        for ticket in self:
            if ticket.company_id and ticket.branch_id:
                ok = bool(self.env['res.company'].search([
                    ('id', 'child_of', ticket.company_id.id),
                    ('id', '=', ticket.branch_id.id),
                ], limit=1))
                if not ok:
                    raise ValidationError(
                        _('Branch must be a child company of the selected Company.'))

    # -------------------------------------------------------------------------
    # SLA — category-aware matching
    # -------------------------------------------------------------------------

    def _sla_find_extra_domain(self):
        # keep default partner-based domain; category filtering is enforced in _sla_find
        return super()._sla_find_extra_domain()

    def _sla_find(self):
        """Category-aware SLA domain: SLAs apply if (no categories) OR (SLA.category is an ancestor of ticket.category)."""
        tickets_map, sla_domain_map = {}, {}

        # Cache ancestor sets per category to avoid repeated searches
        cat_ancestor_cache = {}

        def _generate_key(ticket):
            fields_list = self._sla_reset_trigger()
            key_parts = []
            for field_name in fields_list:
                if ticket._fields[field_name].type == 'many2one':
                    key_parts.append(ticket[field_name].id)
                else:
                    key_parts.append(ticket[field_name])
            return tuple(key_parts)

        for ticket in self:
            if getattr(ticket, 'sla_opt_out', False):
                continue
            if not ticket.team_id.use_sla:
                continue

            key = _generate_key(ticket)
            tickets_map.setdefault(key, self.env['helpdesk.ticket'])
            tickets_map[key] |= ticket

            if key not in sla_domain_map:
                # If ticket has explicit SLA set, restrict search to those ids (should be one)
                if ticket.sla_ids:
                    base_domain = [('id', 'in', ticket.sla_ids.ids)]
                else:
                    base_domain = [
                        '|', ('team_id', '=', False), ('team_id',
                                                       '=', ticket.team_id.id),
                        '|', ('priority_all', '=', True), ('priority',
                                                           '=', ticket.priority),
                        ('stage_id.sequence', '>=', ticket.stage_id.sequence),
                    ]

                    if ticket.category_id:
                        cat_id = ticket.category_id.id
                        if cat_id not in cat_ancestor_cache:
                            cat_ancestor_cache[cat_id] = self.env['helpdesk.category'].search(
                                [('id', 'parent_of', cat_id)]
                            ).ids
                        ancestor_ids = cat_ancestor_cache[cat_id]
                        base_domain += ['|', ('category_ids', '=', False),
                                        ('category_ids', 'in', ancestor_ids)]
                    else:
                        base_domain += [('category_ids', '=', False)]

                extra = Domain.OR(
                    [ticket._sla_find_extra_domain(), self._sla_find_false_domain()])
                sla_domain_map[key] = Domain.AND([base_domain, extra])

        result = {}
        for key, tickets in tickets_map.items():
            domain = sla_domain_map[key]
            slas = self.env['helpdesk.sla'].search(domain)  # respect ACLs
            result[tickets] = slas.filtered(
                lambda s: not s.tag_ids or (tickets.tag_ids & s.tag_ids))
        return result

    @api.model
    def _sla_reset_trigger(self):
        base = super()._sla_reset_trigger()
        return list(dict.fromkeys(base + ['category_id', 'sla_recompute_bump', 'sla_opt_out']))

    def _bump_sla(self):
        for ticket in self:
            ticket.sla_recompute_bump = (ticket.sla_recompute_bump or 0) + 1

    # ---------------------------------------------------------------------
    # Actions
    # ---------------------------------------------------------------------

    def _cleanup_obsolete_sla_statuses_after_change(self, vals):
        """Delete SLA statuses whose SLA no longer matches the ticket after changes.

        Core keeps reached statuses when recomputing (keep_reached=True). This ensures
        we don't keep statuses pointing to SLAs that no longer apply to the ticket.
        """
        trigger_fields = set(self._sla_reset_trigger())
        if not (set(vals.keys()) & trigger_fields):
            return
        sla_map = self._sla_find()
        # Build a per-ticket allowed SLA id set
        allowed_by_ticket = {}
        for tickets, slas in sla_map.items():
            allowed_ids = set(slas.ids)
            for t in tickets:
                allowed_by_ticket[t.id] = allowed_ids
        to_unlink = self.env['helpdesk.sla.status']
        for ticket in self:
            allowed = allowed_by_ticket.get(ticket.id, set())
            if not allowed:
                # No SLA should apply anymore, remove all statuses
                to_unlink |= ticket.sudo().sla_status_ids
            else:
                to_unlink |= ticket.sudo().sla_status_ids.filtered(
                    lambda st: st.sla_id.id not in allowed)
        if to_unlink:
            to_unlink.unlink()

    # -------------------------------------------------------------------------
    # Defaults
    # -------------------------------------------------------------------------

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'partner_id' in fields_list and not res.get('partner_id'):
            if self.env.user.partner_id:
                res['partner_id'] = self.env.user.partner_id.id
        return res
