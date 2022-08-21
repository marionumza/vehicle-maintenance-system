# Copyright 2016-2022, Jarsa
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class VmsOrder(models.Model):
    _description = "VMS Orders"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _name = "vms.order"

    name = fields.Char(string="Order Number", readonly=True)
    supervisor_id = fields.Many2one(
        "hr.employee",
        string="Supervisor",
        domain=[("mechanic", "=", True)],
    )
    date = fields.Datetime(required=True, default=fields.Datetime.now, copy=False)
    current_odometer = fields.Float(copy=False)
    type = fields.Selection(
        [("preventive", "Preventive"), ("corrective", "Corrective")], required=True
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse",
        required=True,
        default=lambda self: self._default_warehouse_id(),
    )
    start_date = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        string="Schedule start",
        copy=False,
    )
    end_date = fields.Datetime(
        required=True,
        compute="_compute_end_date",
        string="Schedule end",
    )
    start_date_real = fields.Datetime(
        readonly=True, string="Real start date", copy=False
    )
    end_date_real = fields.Datetime(
        compute="_compute_end_date_real", readonly=True, string="Real end date"
    )
    order_line_ids = fields.One2many(
        "vms.order.line",
        "order_id",
        string="Order Lines",
    )
    program_id = fields.Many2one("vms.program")
    report_ids = fields.Many2many("vms.report", string="Report(s)")
    report_count = fields.Integer(
        string="Reports",
        compute="_compute_report_count",
        copy=False,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("open", "Open"),
            ("released", "Released"),
            ("cancel", "Cancel"),
        ],
        readonly=True,
        default="draft",
        tracking=True,
    )
    unit_id = fields.Many2one("fleet.vehicle", required=True)
    picking_ids = fields.One2many(
        "stock.picking",
        "vms_order_id",
        string="Stock Pickings",
        copy=False,
    )
    picking_count = fields.Integer(
        string="Delivery Orders",
        compute="_compute_pickings_count",
    )
    procurement_group_id = fields.Many2one(
        "procurement.group",
        string="Procurement Group",
        readonly=True,
        copy=False,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )

    @api.depends("report_ids")
    def _compute_report_count(self):
        for rec in self:
            rec.report_count = len(rec.report_ids)

    @api.depends("picking_ids")
    def _compute_picking_count(self):
        for rec in self:
            rec.picking_count = len(rec.picking_ids)

    def action_view_pickings(self):
        action = self.env["ir.actions.actions"]._for_xml_id(
            "stock.action_picking_tree_all"
        )

        pickings = self.mapped("picking_ids")
        if len(pickings) > 1:
            action["domain"] = [("id", "in", pickings.ids)]
        elif pickings:
            action["views"] = [(self.env.ref("stock.view_picking_form").id, "form")]
            action["res_id"] = pickings.id
        return action

    @api.model
    def _get_warehouse_domain(self):
        return [("company_id", "=", self.env.user.company_id.id)]

    @api.model
    def _default_warehouse_id(self):
        return self.env["stock.warehouse"].search(self._get_warehouse_domain(), limit=1)

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("name", _("/")) == _("/"):
                values["name"] = self.env["ir.sequence"].next_by_code("vms.order")
        return super().create(vals_list)

    @api.depends("order_line_ids")
    def _compute_end_date_real(self):
        for rec in self:
            end_date_real = False
            if rec.start_date_real:
                sum_time = sum(
                    rec.order_line_ids.filtered(lambda l: l.state == "done").mapped(
                        "real_duration"
                    )
                )
                end_date_real = rec.start_date_real + timedelta(hours=sum_time)
            rec.end_date_real = end_date_real

    def action_released(self):
        for order in self:
            order.order_line_ids.action_done()
            if order.type == "corrective":
                order.report_ids.write({"state": "closed"})
            order.write(
                {
                    "state": "released",
                }
            )

    def get_tasks_from_cycle(self, cycle_id, order_id):
        spares = []
        for cycle in cycle_id:
            for task in cycle.task_ids:
                duration = task.duration
                start_date = fields.Datetime.from_string(fields.Datetime.now())
                end_date = start_date + timedelta(hours=duration)
                for spare_part in task.spare_part_ids:
                    spares.append(
                        (
                            0,
                            False,
                            {
                                "product_id": spare_part.product_id.id,
                                "product_qty": spare_part.product_qty,
                                "product_uom_id": (spare_part.product_uom_id.id),
                            },
                        )
                    )
                order_id.order_line_ids += order_id.order_line_ids.new(
                    {
                        "task_id": task.id,
                        "start_date": start_date,
                        "duration": duration,
                        "end_date": end_date,
                        "spare_part_ids": spares,
                        "order_id": order_id.id,
                    }
                )
            if cycle.cycle_ids:
                for sub_cycle in cycle.cycle_ids:
                    order_id.get_tasks_from_cycle(sub_cycle, order_id)
            else:
                break

    @api.onchange("type", "unit_id")
    def _onchange_type(self):
        if self.type == "preventive":
            self.program_id = self.unit_id.program_id
            self.current_odometer = self.unit_id.odometer
            for cycle in self.program_id.cycle_ids:
                self.get_tasks_from_cycle(cycle, self)
        else:
            self.program_id = False
            self.current_odometer = False
            self.order_line_ids = False

    @api.depends("order_line_ids")
    def _compute_end_date(self):
        for rec in self:
            sum_time = 0.0
            if rec.start_date:
                for line in rec.order_line_ids:
                    sum_time += line.duration
                strp_date = fields.Datetime.from_string(rec.start_date)
                rec.end_date = strp_date + timedelta(hours=sum_time)

    def action_open(self):
        for rec in self:
            orders = self.search_count(
                [
                    ("unit_id", "=", rec.unit_id.id),
                    ("state", "=", "open"),
                    ("id", "!=", rec.id),
                ]
            )
            if orders > 0:
                raise ValidationError(
                    _(
                        "Unit not available for maintenance because it has more "
                        "open order."
                    )
                )
            if not rec.order_line_ids:
                raise ValidationError(_("The order must have at least one task"))
            rec.write(
                {
                    "state": "open",
                    "start_date_real": fields.Datetime.now(),
                }
            )
            rec.state = "open"
            if rec.type == "corrective":
                rec.report_ids.write({"state": "pending"})
            rec.order_line_ids.action_process()

    def action_cancel(self):
        for rec in self:
            rec.order_line_ids.action_cancel()
            if rec.type == "corrective":
                rec.report_ids.write(
                    {
                        "state": "pending",
                    }
                )
            rec.state = "cancel"

    def action_cancel_draft(self):
        for rec in self:
            rec.state = "draft"
            if rec.type == "corrective":
                rec.report_ids.write(
                    {
                        "state": "pending",
                    }
                )
            rec.order_line_ids.write(
                {
                    "state": "draft",
                }
            )
