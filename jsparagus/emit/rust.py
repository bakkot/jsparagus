"""Emit code and parser tables in Rust."""

import json
import re
import unicodedata
import sys
import itertools

from ..runtime import (ERROR, ErrorToken, SPECIAL_CASE_TAG)
from ..ordered import OrderedSet

from ..grammar import (CallMethod, Some, is_concrete_element, Nt, InitNt, Optional, End, ErrorSymbol)
from ..actions import Action, Reduce, Lookahead, CheckNotOnNewLine, FilterFlag, PushFlag, PopFlag, FunCall, Seq

from .. import types


TERMINAL_NAMES = {
    '{': 'OpenBrace',
    '}': 'CloseBrace',
    '(': 'OpenParenthesis',
    ')': 'CloseParenthesis',
    '[': 'OpenBracket',
    ']': 'CloseBracket',
    '+': 'Plus',
    '-': 'Minus',
    '~': 'BitwiseNot',
    '!': 'LogicalNot',
    '++': 'Increment',
    '--': 'Decrement',
    ':': 'Colon',
    '=>': 'Arrow',
    '=': 'EqualSign',
    '*=': 'MultiplyAssign',
    '/=': 'DivideAssign',
    '%=': 'RemainderAssign',
    '+=': 'AddAssign',
    '-=': 'SubtractAssign',
    '<<=': 'LeftShiftAssign',
    '>>=': 'SignedRightShiftAssign',
    '>>>=': 'UnsignedRightShiftAssign',
    '&=': 'BitwiseAndAssign',
    '^=': 'BitwiseXorAssign',
    '|=': 'BitwiseOrAssign',
    '**=': 'ExponentiateAssign',
    '.': 'Dot',
    '**': 'Exponentiate',
    '?.': 'OptionalChain',
    '?': 'QuestionMark',
    '??': 'Coalesce',
    '*': 'Star',
    '/': 'Divide',
    '%': 'Remainder',
    '<<': 'LeftShift',
    '>>': 'SignedRightShift',
    '>>>': 'UnsignedRightShift',
    '<': 'LessThan',
    '>': 'GreaterThan',
    '<=': 'LessThanOrEqualTo',
    '>=': 'GreaterThanOrEqualTo',
    '==': 'LaxEqual',
    '!=': 'LaxNotEqual',
    '===': 'StrictEqual',
    '!==': 'StrictNotEqual',
    '&': 'BitwiseAnd',
    '^': 'BitwiseXor',
    '|': 'BitwiseOr',
    '&&': 'LogicalAnd',
    '||': 'LogicalOr',
    ',': 'Comma',
    '...': 'Ellipsis',
}

class RustParserWriter:
    def __init__(self, out, pt, fallible_methods, parser_traits):
        self.out = out
        self.fallible_methods = fallible_methods
        self.parser_traits = parser_traits
        self.parse_table = pt
        self.states = pt.states
        self.shift_count = pt.count_shift_states()
        self.action_count = pt.count_action_states()
        self.init_state_map = pt.named_goals
        self.terminals = list(OrderedSet(pt.terminals))
        # This extra terminal is used to represent any ErrorySymbol transition,
        # knowing that we assert that there is only one ErrorSymbol kind per
        # state.
        self.terminals.append("ErrorToken")
        self.nonterminals = list(OrderedSet(pt.nonterminals))

    def emit(self):
        self.header()
        self.terms_id()
        self.shift()
        self.error_codes()
        self.check_camel_case()
        self.parser_trait()
        self.actions()
        self.reduce()
        self.reduce_simulator()
        self.entry()

    def write(self, indentation, string, *format_args):
        if len(format_args) == 0:
            formatted = string
        else:
            formatted = string.format(*format_args)
        self.out.write("    " * indentation + formatted + "\n")

    def header(self):
        self.write(0, "// WARNING: This file is autogenerated.")
        self.write(0, "")
        self.write(0, "use crate::ast_builder::AstBuilderDelegate;")
        self.write(0, "use crate::stack_value_generated::{StackValue, TryIntoStack};")
        self.write(0, "use crate::error::Result;")
        self.write(0, "")
        self.write(0, "const ERROR: i64 = {};", hex(ERROR))
        self.write(0, "")


    def terminal_name(self, value):
        if isinstance(value, End) or value is None:
            return "End"
        elif isinstance(value, ErrorSymbol) or value is ErrorToken:
            return "ErrorToken"
        elif value in TERMINAL_NAMES:
            return TERMINAL_NAMES[value]
        elif value.isalpha():
            if value.islower():
                return value.capitalize()
            else:
                return value
        else:
            raw_name = " ".join((unicodedata.name(c) for c in value))
            snake_case = raw_name.replace("-", " ").replace(" ", "_").lower()
            camel_case = self.to_camel_case(snake_case)
            return camel_case

    def terminal_name_camel(self, value):
        return self.to_camel_case(self.terminal_name(value))

    def terms_id(self):
        self.write(0, "#[derive(Copy, Clone, Debug, PartialEq)]")
        self.write(0, "pub enum TerminalId {")
        for i, t in enumerate(self.terminals):
            name = self.terminal_name(t)
            self.write(1, "{} = {}, // {}", name, i, repr(t))
        self.write(0, "}")
        self.write(0, "")
        self.write(0, "#[derive(Clone, Copy, Debug, PartialEq)]")
        self.write(0, "pub enum NonterminalId {")
        offset = len(self.terminals)
        for i, nt in enumerate(self.nonterminals):
            self.write(1, "{} = {},", self.nonterminal_to_camel(nt), i + offset)
        self.write(0, "}")
        self.write(0, "")
        self.write(0, "#[derive(Clone, Copy, Debug, PartialEq)]")
        self.write(0, "pub enum Term {")
        self.write(1, "Terminal(TerminalId),")
        self.write(1, "Nonterminal(NonterminalId),")
        self.write(0, "}")
        self.write(0, "")
        self.write(0, "impl From<Term> for usize {")
        self.write(1, "fn from(term: Term) -> Self {")
        self.write(2, "match term {")
        self.write(3, "Term::Terminal(t) => t as usize,")
        self.write(3, "Term::Nonterminal(nt) => nt as usize,")
        self.write(2, "}")
        self.write(1, "}")
        self.write(0, "}")
        self.write(0, "")
        self.write(0, "impl From<Term> for &'static str {")
        self.write(1, "fn from(term: Term) -> Self {")
        self.write(2, "match term {")
        for t in self.terminals:
            name = self.terminal_name(t)
            self.write(3, "Term::Terminal(TerminalId::{}) => &\"{}\",", name, repr(t))
        for nt in self.nonterminals:
            name = self.nonterminal_to_camel(nt)
            self.write(3, "Term::Nonterminal(NonterminalId::{}) => &\"{}\",", name, str(nt.name))
        self.write(2, "}")
        self.write(1, "}")
        self.write(0, "}")
        self.write(0, "")

    def shift(self):
        self.write(0, "#[rustfmt::skip]")
        width = len(self.terminals) + len(self.nonterminals)
        num_shifted_edges = 0
        def state_get(state, t):
            nonlocal num_shifted_edges
            res = state.get(t, "ERROR")
            if res == "ERROR":
                error_symbol = state.get_error_symbol()
                if t == "ErrorToken" and error_symbol:
                    res = state[error_symbol]
                    num_shifted_edges += 1
            else:
                num_shifted_edges += 1
            return res
        self.write(0, "static SHIFT: [i64; {}] = [", self.shift_count * width)
        assert self.terminals[-1] == "ErrorToken"
        for i, state in enumerate(self.states[:self.shift_count]):
            num_shifted_edges = 0
            self.write(1, "// {}.", i)
            for ctx in self.parse_table.debug_context(state.index, None):
                self.write(1, "// {}", ctx)
            self.write(1, "{}",
                       ' '.join("{},".format(state_get(state, t)) for t in self.terminals))
            self.write(1, "{}",
                       ' '.join("{},".format(state_get(state, t)) for t in self.nonterminals))
            try:
                assert sum(1 for _ in state.shifted_edges()) == num_shifted_edges
            except:
                print("Some edges are not encoded.")
                print("List of terminals: {}".format(', '.join(map(repr, self.terminals))))
                print("List of nonterminals: {}".format(', '.join(map(repr, self.nonterminals))))
                print("State having the issue: {}".format(str(state)))
                raise
        self.write(0, "];")
        self.write(0, "")

    def render_action(self, action):
        if isinstance(action, tuple):
            if action[0] == 'IfSameLine':
                _, a1, a2 = action
                if a1 is None:
                    a1 = 'ERROR'
                if a2 is None:
                    a2 = 'ERROR'
                index = self.add_special_case(
                    "if token.is_on_new_line { %s } else { %s }"
                    % (a2, a1))
            else:
                raise ValueError("unrecognized kind of special case: {!r}".format(action))
            return SPECIAL_CASE_TAG + index
        elif action == 'ERROR':
            return action
        else:
            assert isinstance(action, int)
            return action

    def emit_special_cases(self):
        self.write(0, "static SPECIAL_CASES: [fn(&Token<'_>) -> i64; {}] = [",
                   len(self.special_cases))
        for i, code in enumerate(self.special_cases):
            self.write(1, "|token| {{ {} }},", code)
        self.write(0, "];")
        self.write(0, "")

    def error_codes(self):
        self.write(0, "#[derive(Clone, Copy, Debug, PartialEq)]")
        self.write(0, "pub enum ErrorCode {")
        error_symbols = (s.get_error_symbol() for s in self.states[:self.shift_count])
        error_codes = (e.error_code for e in error_symbols if e is not None)
        for error_code in OrderedSet(error_codes):
            self.write(1, "{},", self.to_camel_case(error_code))
        self.write(0, "}")
        self.write(0, "")

        self.write(0, "static STATE_TO_ERROR_CODE: [Option<ErrorCode>; {}] = [",
                   self.shift_count)
        for i, state in enumerate(self.states[:self.shift_count]):
            error_symbol = state.get_error_symbol()
            if error_symbol is None:
                self.write(1, "None,")
            else:
                self.write(1, "// {}.", i)
                for ctx in self.parse_table.debug_context(state.index, None):
                    self.write(1, "// {}", ctx)
                self.write(1, "Some(ErrorCode::{}),",
                           self.to_camel_case(error_symbol.error_code))
        self.write(0, "];")
        self.write(0, "")

    def nonterminal_to_snake(self, ident):
        if isinstance(ident, Nt):
            if isinstance(ident.name, InitNt):
                name = "Start" + ident.name.goal.name
            else:
                name = ident.name
            base_name = self.to_snek_case(name)
            args = ''.join((("_" + self.to_snek_case(name))
                            for name, value in ident.args if value))
            return base_name + args
        else:
            assert isinstance(ident, str)
            return self.to_snek_case(ident)

    def nonterminal_to_camel(self, nt):
        return self.to_camel_case(self.nonterminal_to_snake(nt))

    def to_camel_case(self, ident):
        if '_' in ident:
            return ''.join(word.capitalize() for word in ident.split('_'))
        elif ident.islower():
            return ident.capitalize()
        else:
            return ident

    def check_camel_case(self):
        seen = {}
        for nt in self.nonterminals:
            cc = self.nonterminal_to_camel(nt)
            if cc in seen:
                raise ValueError("{} and {} have the same camel-case spelling ({})".format(
                    seen[cc], nt, cc))
            seen[cc] = nt

    def to_snek_case(self, ident):
        # https://stackoverflow.com/questions/1175208
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', ident)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

    def method_name_to_rust(self, name):
        """Convert jsparagus's internal method name to idiomatic Rust."""
        nt_name, space, number = name.partition(' ')
        name = self.nonterminal_to_snake(nt_name)
        if space:
            name += "_p" + str(number)
        return name

    def get_associated_type_names(self):
        names = OrderedSet()

        def visit_type(ty):
            for arg in ty.args:
                visit_type(arg)
            if len(ty.args) == 0:
                names.add(ty.name)

        for ty in self.grammar.nt_types:
            visit_type(ty)
        for method in self.grammar.methods.values():
            visit_type(method.return_type)
        return names

    def type_to_rust(self, ty, namespace, boxed=False):
        """
        Convert a jsparagus type (see types.py) to Rust.

        Pass boxed=True if the type needs to be boxed.
        """
        if ty == types.UnitType:
            assert not boxed
            rty = '()'
        elif ty == types.TokenType:
            rty = "Token<'alloc>"
        elif ty.name == 'Option' and len(ty.args) == 1:
            # We auto-translate `Box<Option<T>>` to `Option<Box<T>>` since
            # that's basically the same thing but more efficient.
            [arg] = ty.args
            return 'Option<{}>'.format(self.type_to_rust(arg, namespace, boxed))
        elif ty.name == 'Vec' and len(ty.args) == 1:
            [arg] = ty.args
            rty = "Vec<'alloc, {}>".format(self.type_to_rust(arg, namespace, boxed=False))
        else:
            if namespace == "":
                rty = ty.name
            else:
                rty = namespace + '::' + ty.name
            if ty.args:
                rty += '<{}>'.format(', '.join(self.type_to_rust(arg, namespace, boxed)
                                               for arg in ty.args))
        if boxed:
            return "Box<'alloc, {}>".format(rty)
        else:
            return rty

    def handler_trait(self):
        # NOTE: unused, code kept if we need it later
        self.write(0, "pub trait Handler {")

        for name in self.get_associated_type_names():
            self.write(1, "type {};", name)

        for tag, method in self.grammar.methods.items():
            method_name = self.method_name_to_rust(tag)
            arg_types = [
                self.type_to_rust(ty, "Self")
                for ty in method.argument_types
                if ty != types.UnitType
            ]
            if method.return_type == types.UnitType:
                return_type_tag = ''
            else:
                return_type_tag = ' -> ' + \
                    self.type_to_rust(method.return_type, "Self")

            args = ", ".join(("a{}: {}".format(i, t)
                              for i, t in enumerate(arg_types)))
            self.write(1, "fn {}(&self, {}){};",
                       method_name, args, return_type_tag)
        self.write(0, "}")
        self.write(0, "")

    def element_type(self, e):
        # Mostly duplicated from types.py. :(
        g = self.grammar
        if isinstance(e, str):
            return types.TokenType
        elif isinstance(e, Optional):
            return types.Type('Option', [self.element_type(e.inner)])
        elif isinstance(e, Nt):
            # Cope with the awkward fact that g.nonterminals keys may be either
            # strings or Nt objects.
            nt_key = e if e in g.nonterminals else e.name
            assert g.nonterminals[nt_key].type is not None
            return g.nonterminals[nt_key].type
        else:
            assert False, "unexpected element type: {!r}".format(e)

    def parser_trait(self):
        self.write(0, "pub struct TermValue<Value> {")
        self.write(1, "pub term: Term,")
        self.write(1, "pub value: Value,")
        self.write(0, "}")
        self.write(0, "")
        self.write(0, "pub trait ParserTrait<'alloc, Value> {")
        self.write(1, "fn shift(&mut self, tv: TermValue<Value>) -> Result<'alloc, bool>;")
        self.write(1, "fn replay(&mut self, tv: TermValue<Value>);")
        self.write(1, "fn epsilon(&mut self, state: usize);")
        self.write(1, "fn pop(&mut self) -> TermValue<Value>;")
        self.write(1, "fn check_not_on_new_line(&self, peek: usize) -> Result<'alloc, bool>;")
        self.write(0, "}")
        self.write(0, "")

    def actions(self):
        if not self.parse_table:
            return
        has_ast_builder = False
        used_offsets = set()

        def collect_offsets(act):
            # Given an action, returns the list of used stack slots.
            assert isinstance(act, Action)
            if isinstance(act, Reduce):
                for i in reversed(range(act.replay)):
                    offset = i + 1
                    yield offset
            elif isinstance(act, FunCall):
                def map_with_offset(args):
                    for a in args:
                        if isinstance(a, int):
                            yield a + act.offset
                        elif isinstance(a, Some):
                            for offset in map_with_offset([a.inner]):
                                yield offset
                if has_ast_builder or act.method == "id":
                    for offset in map_with_offset(act.args):
                        yield offset
            elif isinstance(act, Seq):
                for a in act.actions:
                    for offset in collect_offsets(a):
                        yield offset

        def write_action(indent, act, is_packed):
            # Compile function calls and reduce actions to Rust. Return whether
            # the control flow exit (False) or fallthrough (True).
            assert isinstance(act, Action)
            assert not act.is_inconsistent()
            if isinstance(act, Reduce):
                value = "value"
                try:
                    packed = is_packed[value]
                except:
                    packed = False
                    value = "None"
                if packed:
                    value = "{}.value".format(value)
                else:
                    if has_ast_builder:
                        value = "TryIntoStack::try_into_stack({})?".format(value)
                    else:
                        value = "value"

                replay_list = []
                self.write(indent, "let term = Term::Nonterminal(NonterminalId::{});",
                           self.nonterminal_to_camel(act.nt))
                if value != "value":
                    self.write(indent, "let value = {};", value)
                for i in range(act.replay):
                    self.write(indent, "parser.replay(s{});", i + 1)
                self.write(indent, "parser.replay(TermValue { term, value });")
                self.write(indent, "Ok(false)")
                return False
            elif isinstance(act, CheckNotOnNewLine):
                self.write(indent, "parser.check_not_on_new_line({})?;", 1 - act.offset)
            elif isinstance(act, Lookahead):
                raise ValueError("Unexpected Lookahead action")
            elif isinstance(act, FilterFlag):
                raise ValueError("NYI: FilterFlag action")
            elif isinstance(act, PushFlag):
                raise ValueError("NYI: PushFlag action")
            elif isinstance(act, PopFlag):
                raise ValueError("NYI: PopFlag action")
            elif isinstance(act, FunCall):
                def no_unpack(val):
                    return val
                def unpack(val):
                    try:
                        packed = is_packed[val]
                    except:
                        packed = True
                    if packed:
                        return "{}.value.to_ast()?".format(val)
                    return val
                def map_with_offset(args, unpack):
                    get_value = "s{}"
                    for a in args:
                        if isinstance(a, int):
                            yield unpack(get_value.format(a + act.offset))
                        elif isinstance(a, str):
                            yield unpack(a)
                        elif isinstance(a, Some):
                            yield "Some({})".format(next(map_with_offset([a.inner], unpack)))
                        elif a is None:
                            yield "None"
                        else:
                            raise ValueError(a)
                packed = False
                if act.method == "id":
                    assert len(act.args) == 1
                    self.write(indent, "let {} = {};", act.set_to, next(map_with_offset(act.args, no_unpack)))
                    is_packed[act.set_to] = True
                elif act.method == "accept":
                    assert len(act.args) == 0
                    self.write(indent, "return Ok(true);")
                    return False
                elif has_ast_builder:
                    # TODO: Check whether the function is implemented by the
                    # given traits, to decide whether to implement it or not.
                    forward_errors = ""
                    if act.method in self.fallible_methods:
                        forward_errors = "?"
                    self.write(indent, "let {} = parser.ast_builder_refmut().{}({}){};",
                               act.set_to, act.method, ", ".join(map_with_offset(act.args, unpack)),
                               forward_errors)
                    is_packed[act.set_to] = False
                else:
                    if act.set_to == "value":
                        self.write(indent, "let value = ();")
            elif isinstance(act, Seq):
                if act.update_stack():
                    reducer = act.reduce_with()
                    depth = reducer.pop + reducer.replay
                    for i in range(depth):
                        name = 's'
                        if i + 1 not in used_offsets:
                            name = '_s'
                        self.write(indent, "let {}{} = parser.pop();", name, i + 1)

                for a in act.actions:
                    fallthrough = write_action(indent, a, is_packed)
                    if not fallthrough:
                        return False
            else:
                raise ValueError("Unknow action type")
            return True

        # Note use of std::vec::Vec below: we have imported `arena::Vec` in this module,
        # since every other data structure mentioned in this file lives in the arena.
        has_ast_builder = True
        traits = ["ParserTrait<'alloc, StackValue<'alloc>>", "AstBuilderDelegate<'alloc>"]
        self.write(0, "pub fn actions<'alloc, Handler>(parser: &mut Handler, state: usize) -> Result<'alloc, bool>")
        self.write(0, "where")
        self.write(1, "Handler: {}", ' + '.join(traits))
        self.write(0, "{")
        self.write(1, "match state {")
        assert len(self.states[self.shift_count:]) == self.action_count
        for state in self.states[self.shift_count:]:
            self.write(2, "{} => {{", state.index)
            for ctx in self.parse_table.debug_context(state.index, None):
                self.write(3, "// {}", ctx)
            for act, d in state.edges():
                self.write(3, "// {} --> {}", repr(act), d)
                is_packed = {} # Map variable names to a boolean to know if the data is packed or not.
                try:
                    used_offsets = set(collect_offsets(act))
                    fallthrough = write_action(3, act, is_packed)
                except:
                    print("Error while writting code for {}\n\n".format(state))
                    self.parse_table.debug_info = True
                    print(self.parse_table.debug_context(state.index, "\n", "# "))
                    raise
                if fallthrough:
                    self.write(3, "parser.epsilon({});", d)
                    self.write(3, "return Ok(false)")
            self.write(2, "}")
        self.write(2, '_ => panic!("no such state: {}", state),')
        self.write(1, "}")
        self.write(0, "}")
        self.write(0, "")
        # Add another implementation which is only reducing and not checking
        # any values.
        used_offsets = set()
        has_ast_builder = False
        traits = ["ParserTrait<'alloc, ()>"]
        self.write(0, "pub fn noop_actions<'alloc, Handler>(parser: &mut Handler, state: usize) -> Result<'alloc, bool>")
        self.write(0, "where")
        self.write(1, "Handler: {}", ' + '.join(traits))
        self.write(0, "{")
        self.write(1, "match state {")
        assert len(self.states[self.shift_count:]) == self.action_count
        for state in self.states[self.shift_count:]:
            self.write(2, "{} => {{", state.index)
            for ctx in self.parse_table.debug_context(state.index, None):
                self.write(3, "// {}", ctx)
            for act, d in state.edges():
                self.write(3, "// {} --> {}", repr(act), d)
                is_packed = {} # Map variable names to a boolean to know if the data is packed or not.
                try:
                    used_offsets = set(collect_offsets(act))
                    fallthrough = write_action(3, act, is_packed)
                except:
                    print("Error while writting code for {}\n\n".format(state))
                    self.parse_table.debug_info = True
                    print(self.parse_table.debug_context(state.index, "\n", "# "))
                    raise
                if fallthrough:
                    self.write(3, "parser.epsilon({});", d)
                    self.write(3, "return Ok(false)")
            self.write(2, "}")
        self.write(2, '_ => panic!("no such state: {}", state),')
        self.write(1, "}")
        self.write(0, "}")
        self.write(0, "")


    def reduce(self):
        if self.parse_table:
            return
        # Note use of std::vec::Vec below: we have imported `arena::Vec` in this module,
        # since every other data structure mentioned in this file lives in the arena.
        self.write(0, "pub fn reduce<'alloc>(")
        self.write(1, "handler: &mut AstBuilder<'alloc>,")
        self.write(1, "prod: usize,")
        self.write(1, "stack: &mut std::vec::Vec<StackValue<'alloc>>,")
        self.write(0, ") -> Result<'alloc, NonterminalId> {")
        self.write(1, "match prod {")
        for i, prod in enumerate(self.prods):
            # If prod.nt is not in nonterminals, that means it's a goal
            # nonterminal, only accepted, never reduced.
            if prod.nt in self.nonterminals:
                self.write(2, "{} => {{", i)
                self.write(3, "// {}",
                           self.grammar.production_to_str(prod.nt, prod.rhs, prod.reducer))

                # At run time, the top of the stack will be one value per
                # concrete symbol in the RHS of the production we're reducing.
                # We are about to emit code to pop these values from the stack,
                # one at a time. They come off the stack in reverse order.
                elements = [e for e in prod.rhs if is_concrete_element(e)]

                # We can emit three different kinds of code here:
                #
                # 1.  Full compilation. Pop each value from the stack; if it's
                #     used, downcast it to its actual type and store it in a
                #     local variable (otherwise just drop it). Then, evaulate
                #     the reduce-expression. Push the result back onto the
                #     stack.
                #
                # 2.  `is_discarding_reduction`: A reduce expression that is
                #     just an integer is retaining one stack value and dropping
                #     the rest. We skip the downcast in this case.
                #
                # 3.  `is_trivial_reduction`: A production has only one
                #     concrete symbol in it, and the reducer is just `0`.
                #     We don't have to do anything at all here.
                is_trivial_reduction = len(elements) == 1 and prod.reducer == 0
                is_discarding_reduction = isinstance(prod.reducer, int)

                # While compiling, figure out which elements are used.
                variable_used = [False] * len(elements)

                def compile_reduce_expr(expr):
                    """Compile a reduce expression to Rust"""
                    if isinstance(expr, CallMethod):
                        method_type = self.grammar.methods[expr.method]
                        method_name = self.method_name_to_rust(expr.method)
                        assert len(method_type.argument_types) == len(expr.args)

                        # Given arguments can contain any mutable call,
                        # store them in local variable first.
                        arg_defs = ''
                        args = ''
                        i = 0
                        for ty, arg in zip(method_type.argument_types,
                                           expr.args):
                            if ty != types.UnitType:
                                arg_defs += 'let a{} = {};'.format(i, compile_reduce_expr(arg))
                                args += 'a{},'.format(i)
                                i += 1
                        call = "{{ {} handler.{}({}) }}".format(
                            arg_defs, method_name, args)

                        # Extremely bad hack. In Rust, since type inference is
                        # currently so poor, we don't have enough information
                        # to know if this method can fail or not, and Rust
                        # requires us to know that.
                        if method_name in self.fallible_methods:
                            call += "?"
                        return call
                    elif isinstance(expr, Some):
                        return "Some({})".format(compile_reduce_expr(expr.inner))
                    elif expr is None:
                        return "None"
                    else:
                        # can't be 'accept' because we filter out InitNt productions
                        assert isinstance(expr, int)
                        variable_used[expr] = True
                        return "x{}".format(expr)

                compiled_expr = compile_reduce_expr(prod.reducer)

                if not is_trivial_reduction:
                    for index, e in reversed(list(enumerate(elements))):
                        if variable_used[index]:
                            ty = self.element_type(e)
                            rust_ty = self.type_to_rust(ty, "", boxed=True)
                            if is_discarding_reduction:
                                self.write(3, "let x{} = stack.pop().unwrap();", index)
                            else:
                                self.write(3, "let x{}: {} = stack.pop().unwrap().to_ast()?;", index, rust_ty)
                        else:
                            self.write(3, "stack.pop();", index)

                    if is_discarding_reduction:
                        self.write(3, "stack.push({});", compiled_expr)
                    else:
                        self.write(3, "stack.push(TryIntoStack::try_into_stack({})?);", compiled_expr)

                self.write(3, "Ok(NonterminalId::{})",
                           self.nonterminal_to_camel(prod.nt))
                self.write(2, "}")
        self.write(2, '_ => panic!("no such production: {}", prod),')
        self.write(1, "}")
        self.write(0, "}")
        self.write(0, "")

    def reduce_simulator(self):
        if self.parse_table:
            return
        prods = [prod for prod in self.prods if prod.nt in self.nonterminals]
        self.write(0, "static REDUCE_SIMULATOR: [(usize, NonterminalId); {}] = [", len(prods))
        for prod in prods:
            elements = [e for e in prod.rhs if is_concrete_element(e)]
            self.write(1, "({}, NonterminalId::{}),", len(elements), self.nonterminal_to_camel(prod.nt))
        self.write(0, "];")
        self.write(0, "")

    def entry(self):
        self.write(0, "#[derive(Clone, Copy)]")
        self.write(0, "pub struct ParseTable<'a> {")
        self.write(1, "pub shift_count: usize,")
        self.write(1, "pub action_count: usize,")
        self.write(1, "pub shift_table: &'a [i64],")
        self.write(1, "pub shift_width: usize,")
        self.write(1, "pub error_codes: &'a [Option<ErrorCode>],")
        self.write(0, "}")
        self.write(0, "")

        self.write(0, "impl<'a> ParseTable<'a> {")
        self.write(1, "pub fn check(&self) {")
        self.write(2, "assert_eq!(")
        self.write(3, "self.shift_table.len(),")
        self.write(3, "(self.shift_count * self.shift_width) as usize")
        self.write(2, ");")
        self.write(1, "}")
        self.write(0, "}")
        self.write(0, "")

        self.write(0, "pub static TABLES: ParseTable<'static> = ParseTable {")
        self.write(1, "shift_count: {},", self.shift_count)
        self.write(1, "action_count: {},", self.action_count)
        self.write(1, "shift_table: &SHIFT,")
        self.write(1, "shift_width: {},", len(self.terminals) + len(self.nonterminals))
        self.write(1, "error_codes: &STATE_TO_ERROR_CODE,")
        self.write(0, "};")
        self.write(0, "")

        for init_nt, index in self.init_state_map:
            assert init_nt.args == ()
            self.write(0, "pub static START_STATE_{}: usize = {};",
                       self.nonterminal_to_snake(init_nt).upper(), index)
            self.write(0, "")


def write_rust_parser_states(out, parser_states, handler_info):
    raise ValueError("Unsupported ParserStates")

def write_rust_parse_table(out, parse_table, handler_info):
    if not handler_info:
        print("WARNING: info.json is not provided", file=sys.stderr)
        fallible_methods = []
        parser_traits = []
    else:
        with open(handler_info, "r") as json_file:
            handler_info_json = json.load(json_file)
        fallible_methods = handler_info_json["fallible-methods"]
        parser_traits = handler_info_json["parser-traits"]

    RustParserWriter(out, parse_table, fallible_methods, parser_traits).emit()
