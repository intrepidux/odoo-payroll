# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
import odoo.addons.decimal_precision as dp
import logging
from odoo import models, fields, api, _
from odoo.exceptions import Warning
from odoo.tools import email_split, float_is_zero
import datetime

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    expense_journal_id = fields.Many2one('account.journal', string='Default Expense Journal', default_model = 'account.journal', config_parameter='hr_expense.expense_journal_id')

#Adding the group portal_read_own_contract to the groups in the contract_id field
class hr_employee(models.Model):
    _inherit = 'hr.employee.public'
    contract_id = fields.Many2one('hr.contract', string='Current Contract',
        groups="hr.group_hr_user,hr_payroll_employeefund.group_contract_holder",domain="[('company_id', '=', company_id)]", help='Current contract of the employee')

class HrEmployee(models.Model):
    _inherit = "hr.employee"
    #Adding the group portal_read_own_contract to the groups in the contract_id field
    contract_id = fields.Many2one('hr.contract', string='Current Contract',
        groups="hr.group_hr_user,hr_payroll_employeefund.group_contract_holder",domain="[('company_id', '=', company_id)]", help='Current contract of the employee')

class HrExpenseSheet(models.Model):
    _inherit = "hr.expense.sheet"

    employee_invoice_id = fields.Many2one('account.move', string="Employee invoice", readonly = True)
    employee_fund = fields.Many2one(string="Employee Fund", comodel_name='account.analytic.account', help="Use this account together with marked salary rule", related='employee_id.contract_id.employee_fund')
    employee_fund_balance = fields.Monetary(string='Balance', related='employee_fund.balance', currency_field='currency_id')
    
    #@api.onchange('name')
    #def _compute_reference(self):
    #    for line in self:
    #         _logger.warning(line)
             #line.reference = f"{line.name} - {datetime.datetime.now():%Y-%m-%d %H:%M}"

    @api.model
    def _default_journal_id(self):
        """ The journal is determining the company of the accounting entries generated from expense. We need to force journal company and expense sheet company to be the same. """
        journal = self.env['ir.config_parameter'].sudo().get_param('hr_expense.expense_journal_id', default=1)
        return journal

    journal_id = fields.Many2one('account.journal', string='Expense Journal', states={'done': [('readonly', True)], 'post': [('readonly', True)]}, check_company=True, domain="[('type', '=', 'purchase'), ('company_id', '=', company_id)]",
        default=_default_journal_id, help="The journal used when the expense is done.")

    def approve_expense_sheets(self):
        if self.expense_line_ids[0].date:
            date = self.expense_line_ids[0].date
        else:
            date = fields.Datetime.now()    
        self.accounting_date = date
        super().approve_expense_sheets()
        
    def action_sheet_move_create(self):
        # if self.expense_line_ids[0].payment_mode == 'employee_fund':
        samples = self.mapped('expense_line_ids.sample')
        if samples.count(True):
            if samples.count(False):
                raise UserError(_("You can't mix sample expenses and regular ones"))
            self.write({'state': 'post'})
            return

        if any(sheet.state != 'approve' for sheet in self):
            raise UserError(_("You can only generate accounting entry for approved expense(s)."))

        if any(not sheet.journal_id for sheet in self):
            raise UserError(_("Expenses must have an expense journal specified to generate accounting entries."))

        for sheet in self.filtered(lambda s: not s.accounting_date):
            sheet.accounting_date = sheet.account_move_id.date
        to_post = self.filtered(lambda sheet: sheet.payment_mode == 'own_account' and sheet.expense_line_ids)
        to_post.write({'state': 'post'})
        (self - to_post).write({'state': 'done'})
        self.activity_update()

        if self.expense_line_ids[0].date:
            date = self.accounting_date
        else:
            date = fields.Datetime.now()    
        account_move = self.env['account.move'].with_context(check_move_validity=False).create({
        'ref': self.expense_line_ids[0].reference,
        'move_type': 'in_invoice',
        'partner_id': self.employee_id.address_home_id.id,
        'date': date,
        'invoice_date': date,
        'journal_id': self.journal_id.id,
        })
        for expense_line in self.expense_line_ids:
            line = self.env['account.move.line'].with_context(check_move_validity=False).create({
                'account_id': expense_line.product_id.property_account_expense_id.id,
                'name': expense_line.product_id.name,
                'tax_ids': [expense_line.product_id.supplier_taxes_id.id],
                'quantity': expense_line.quantity,
                'move_id': account_move.id,
                'product_id': expense_line.product_id.id,
                'price_unit': expense_line.unit_amount,
            })
            line._onchange_mark_recompute_taxes()
        #account_move._onchange_partner_id()
        account_move._recompute_dynamic_lines()
        account_move.action_post()
        res = {}
        if self.expense_line_ids[0].payment_mode != 'employee_fund':
            self.account_move_id = account_move.id
            res[self.id] = account_move
        else:
            expense_line_ids = self.mapped('expense_line_ids')\
                .filtered(lambda r: not float_is_zero(r.total_amount, precision_rounding=(r.currency_id or self.env.company.currency_id).rounding))
            res = expense_line_ids.action_move_create_journal(self.employee_id.contract_id.employee_fund_journal_id.id,self.expense_line_ids[0].reference)
            self.employee_invoice_id = account_move.id
        return res
        
    @api.depends(
    'currency_id',
    'account_move_id.line_ids.amount_residual',
    'account_move_id.line_ids.amount_residual_currency',
    'account_move_id.line_ids.account_internal_type',
    'employee_invoice_id.line_ids.amount_residual',
    'employee_invoice_id.line_ids.amount_residual_currency',
    'employee_invoice_id.line_ids.account_internal_type',)
    def _compute_amount_residual(self):
        for record in self:
            if record.payment_mode == "employee_fund":
                for sheet in self:
                    if sheet.currency_id == sheet.company_id.currency_id:
                        residual_field = 'amount_residual'
                    else:
                        residual_field = 'amount_residual_currency'
                    payment_term_lines = sheet.employee_invoice_id.line_ids.filtered(lambda line: line.account_internal_type in ('receivable', 'payable'))
                    sheet.amount_residual = -sum(payment_term_lines.mapped(residual_field))
                    if record.state != "cancel" and record.employee_invoice_id:
                        if sheet.amount_residual  > 0:
                            record.state = "post"
                        else:
                            record.state = "done"
            else:
                    super()._compute_amount_residual()
        
    def action_register_payment(self):
        ''' Open the account.payment.register wizard to pay the selected journal entries.
        :return: An action opening the account.payment.register wizard.
        '''
        for record in self:
            if record.payment_mode == "employee_fund":
                return {
                    'name': _('Register Payment'),
                    'res_model': 'account.payment.register',
                    'view_mode': 'form',
                    'context': {
                        'active_model': 'account.move',
                        'active_ids': self.employee_invoice_id.ids,
                    },
                    'target': 'new',
                    'type': 'ir.actions.act_window',
                }
            else:
                return super().action_register_payment()
        
    @api.onchange('expense_line_ids')
    def _compute_same_date_used(self):
        first_date = False
        for sheet in self:
            for line in sheet.expense_line_ids:
                if line.date and not first_date:
                    first_date = line.date
                if line.date != first_date:
                    raise UserError(_("All expense products do not have the same expense date. If you want to register expenses from different dates, please create separate expense reports"))




class HrExpense(models.Model):
    _inherit = "hr.expense"

    employee_fund = fields.Many2one(string="Employee Fund", comodel_name='account.analytic.account', help="Use this account together with marked salary rule", related='employee_id.contract_id.employee_fund')
    employee_fund_balance = fields.Monetary(string='Balance', related='employee_fund.balance', currency_field='currency_id')
    employee_fund_name = fields.Char(string='Name', related='employee_fund.name')
    payment_mode = fields.Selection(selection_add = [("employee_fund","Kompetensutvecklingsfond")],)
    attachment_reciept_should_be_warned = fields.Boolean(string='If should be given a warning, is given once', default = True)
    
    def action_move_create_journal(self, journal, reference):
        '''
        main function that is called when trying to create the accounting entries related to an expense
        '''
        move_group_by_sheet = self._get_account_move_by_sheet()

        move_line_values_by_expense = self._get_account_move_line_values()

        for expense in self:
            company_currency = expense.company_id.currency_id
            different_currency = expense.currency_id != company_currency

            # get the account move of the related sheet
            move = move_group_by_sheet[expense.sheet_id.id]

            #It a timing issue where the move needs to be created but not posted yet, since after it has been posted we're no longer allowed to change the journal
            move.journal_id = journal # This function is overriden so that can set a journal here so that when it is an employee fund expense then we can set the
            #journal to the same as the contracts employee_fund journal on account_move id on the expense.sheet. If confused ask me (Marcus).
            move.ref = reference #Also to set the ref

            # get move line values
            move_line_values = move_line_values_by_expense.get(expense.id)
            move_line_dst = move_line_values[-1]
            total_amount = move_line_dst['debit'] or -move_line_dst['credit']
            total_amount_currency = move_line_dst['amount_currency']

            # create one more move line, a counterline for the total on payable account
            if expense.payment_mode == 'company_account':
                if not expense.sheet_id.bank_journal_id.default_account_id:
                    raise UserError(_("No account found for the %s journal, please configure one.") % (expense.sheet_id.bank_journal_id.name))
                journal = expense.sheet_id.bank_journal_id
                # create payment
                payment_methods = journal.outbound_payment_method_ids if total_amount < 0 else journal.inbound_payment_method_ids
                journal_currency = journal.currency_id or journal.company_id.currency_id
                payment = self.env['account.payment'].create({
                    'payment_method_id': payment_methods and payment_methods[0].id or False,
                    'payment_type': 'outbound' if total_amount < 0 else 'inbound',
                    'partner_id': expense.employee_id.sudo().address_home_id.commercial_partner_id.id,
                    'partner_type': 'supplier',
                    'journal_id': journal.id,
                    'date': expense.date,
                    'currency_id': expense.currency_id.id if different_currency else journal_currency.id,
                    'amount': abs(total_amount_currency) if different_currency else abs(total_amount),
                    'ref': expense.name,
                })

            # link move lines to move, and move to expense sheet
            move.write({'line_ids': [(0, 0, line) for line in move_line_values]})
            expense.sheet_id.write({'account_move_id': move.id})

            if expense.payment_mode == 'company_account':
                expense.sheet_id.paid_expense_sheets()

        # post the moves
        for move in move_group_by_sheet.values():
            move._post()

        return move_group_by_sheet

    @api.onchange('name')
    def _compute_reference(self):
        for line in self:
            line.reference = f"{line.name} - {datetime.datetime.now():%Y-%m-%d %H:%M}"

    @api.onchange('employee_id', 'payment_mode')
    def _compute_analytic_account(self):
        for line in self:
            if line.payment_mode == 'employee_fund':
                line.analytic_account_id = line.employee_id.contract_id.employee_fund

    def _get_account_move_line_values(self):
        move_line_values_by_expense = super()._get_account_move_line_values()
        keys = move_line_values_by_expense.keys()
        for key in keys:
            expense = self.env['hr.expense'].browse(move_line_values_by_expense[key][0]['expense_id'])
            if expense.payment_mode != 'employee_fund':
                return move_line_values_by_expense
        for key in keys:
            expense = self.env['hr.expense'].browse(move_line_values_by_expense[key][0]['expense_id'])
            price = 0
            new_values = []
            for line in move_line_values_by_expense[key]:
                if 'tax_ids' in line.keys():
                    price = line['debit']
                    line['tax_ids'] = []
                    line['account_id'] = expense.employee_id.contract_id.credit_account_id.id
                if line['credit'] != 0:
                    line['credit'] = price
                    line['account_id'] = expense.employee_id.contract_id.debit_account_id.id
                if 'tax_repartition_line_id' not in line.keys():
                    new_values.append(line)
            move_line_values_by_expense[key] = new_values
        return move_line_values_by_expense

    def _create_sheet_from_expenses(self):
        sheet = super(HrExpense,self)._create_sheet_from_expenses()
        todo = self.filtered(lambda x: x.payment_mode=='employee_fund')
        if len(todo) > 0:
            sheet.name = todo[0].name
            # ~ sheet.name = 'test'
            sheet.expense_line_ids = [(6, 0, todo.ids+sheet.expense_line_ids.ids)]
        return sheet

    def update_analytic_line(self):
        expense_tree = self.env.ref('hr_payroll_employeefund_expenses.quick_view_account_analytic_line_tree')
        ctx = {
            'account_id': self.analytic_account_id.id,
            'partner_id': self.employee_id.address_home_id.id
        }
        return {
            'name': _('Cost and Revenue'),
            'type': 'ir.actions.act_window',
            'view_mode': 'tree,form',
            'res_model': 'account.analytic.line',
            'views': [
                (self.env.ref('hr_payroll_employeefund_expenses.quick_view_account_analytic_line_tree').id, 'tree'),
                (False, 'form')
            ],
            'view_id': expense_tree.id,
            'target': 'new',
            'context': dict(
                account_id=self.analytic_account_id.id,
                partner_id=self.employee_id.address_home_id.id,
                default_account_id=self.analytic_account_id.id,
                default_partner_id=self.employee_id.address_home_id.id,
                default_name=self.name
            ),
            'domain': [
                ('account_id', '=', self.analytic_account_id.id),
                ('partner_id', '=', self.employee_id.address_home_id.id),
            ],
         }


class hr_contract(models.Model):
    _inherit = 'hr.contract'

    credit_account_id = fields.Many2one('account.account', string="Credit Account", default=lambda self: self._get_default_credit_account())
    debit_account_id = fields.Many2one('account.account', string="Debit Account", default=lambda self: self._get_default_debit_account())
    fill_amount = fields.Float(string="Fill Amount", Store=False)
    employee_fund_journal_id = fields.Many2one('account.journal', string="Employeefund Journal")

    def _get_default_credit_account(self):
        return self.env['account.account'].search([('code','=','2829')])

    def _get_default_debit_account(self):
        return self.env['account.account'].search([('code','=','7699')])

    def create_account_move(self):
        if not self.credit_account_id or not self.debit_account_id or not self.employee_fund_journal_id or not self.fill_amount or not self.employee_fund:
            raise UserError(_("Kindly check if credit, debit account, Employeefund Journal, Employee fund are set and the amount to fill employee fund "))
        account_move_line = self.env['account.move.line'].with_context(check_move_validity=False)
        account_move = self.env['account.move'].create({'journal_id': self.employee_fund_journal_id.id})
        account_move_line.create({
            'account_id': self.credit_account_id.id,
            'analytic_account_id': self.employee_fund.id,
            'name': self.employee_id.name,
            'credit': self.fill_amount,
            'exclude_from_invoice_tab': True,
            'move_id': account_move.id,
        })
        account_move_line.create({
            'account_id': self.debit_account_id.id,
            'name': self.employee_id.name,
            'debit': self.fill_amount,
            'exclude_from_invoice_tab': True,
            'move_id': account_move.id,
        })
        account_move.action_post()
        self.fill_amount = 0
