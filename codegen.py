"""Pontos de implementação da geração de comandos e expressões."""

# Visão geral do fluxo:
#   generate  -> percorre as funções do programa e cria uma FunctionIR para cada uma
#   statement -> traduz cada comando (ad hoc)
#   expression -> traduz cada expressão usando a cobertura escolhida pelo Selector

import load_previous
from ir import Context, ProgramIR, FunctionIR
from ast_nodes import (
    Assignment,
    Block,
    CallStmt,
    Expr,
    IfStmt,
    PrintStmt,
    ReturnStmt,
    StringLiteral,
    TypeName,
    VarDecl,
    WhileStmt,
)
import ir as I
from patterns import COND, EFFECT, REG, signed_literal
from selector import Selector
from catalogue import catalogue

class SelectionError(ValueError, RuntimeError):
    """Falta de cobertura ou ação ausente, com a posição do nó no fonte."""

def _where(node):
    # monta a posição do nó para as mensagens de erro
    span = getattr(node, "span", None)
    if span is None:
        return "posição desconhecida"
    return f"linha {span.start_line}, coluna {span.start_column}" 

class CodeGenerator:
    """Coordene comandos ad hoc e seleção de expressões com DP."""

    def __init__(self):
        self.selector = Selector(catalogue()) # guarda as regras do catálogo
        self.program_ir = None  # ProgramIR em construção (funções + pool de strings)
        self.ctx = None         # Context da função atual (temporários, labels, variáveis, emit)
        self._table = None      # SelectionTable da última expressão analisada (cache do DP)

    def generate(self, program):
        """Traduza funções e comandos. Selecione expressões com DP."""
        self.program_ir = ProgramIR()
        self.ctx = None
        self._table = None
        for declaration in program.functions:   # uma função por vez, na ordem do fonte
            self._function(declaration)
        program_ir = self.program_ir    # guarda o resultado antes de limpar o estado
        self.program_ir = self.ctx = self._table = None
        return program_ir

    def _function(self, declaration):
        symbol = declaration.metadata["symbol"] # pega o FunctionSymbol gravado pela análise semântica
        function_ir = FunctionIR(symbol)    # cria a lista de instruções dessa função
        self.program_ir.functions.append(function_ir)   # registra a função no programa
        self.ctx = Context(function_ir, self.program_ir)    # cria contexto da função
        self._table = None  # descarta a tabela de seleção da função anterior
 
        # Um Param por parâmetro, no início, em ordem. O VReg vem do símbolo.
        for index, parameter in enumerate(declaration.parameters):
            register = self.ctx.variable(parameter.metadata["symbol"])  # VReg do parâmetro (pelo símbolo)
            self.ctx.emit(I.Param(register, index, origin=parameter.span))  # índice começa em 0
 
        self.statement(declaration.body)    # gera o corpo da função
 
        # O fim normal de uma função void também é um Return(None). Funções
        # não-void nunca alcançam o fim (garantido pela análise de fluxo).
        if declaration.return_type is TypeName.VOID:
            self.ctx.emit(I.Return(None, origin=declaration.span))        

# Expressões ->
# Uma expressão é traduzida em dois passos:
#   1) o Selector escolhe a regra de menor custo para cada (nó, classe)
#   2) expression() apenas executa a regra escolhida, chamando o método _act_<ação>

    def _lookup(self, node, result_class):
        """Devolva a Choice de (nó, classe), analisando a raiz se necessário."""
        table = self._table
        # se o nó ainda não está na tabela, ele é uma raiz nova: roda o Selector nela
        # (o Selector já cobre todos os filhos, então os filhos reaproveitam a mesma tabela)
        if table is None or id(node) not in table.nodes:
            table = self._table = self.selector.analyze(node)
        return table.lookup(node, result_class)

    def expression(self, node, result_class, true_label=None, false_label=None, dst=None):
        """Selecione e reconstrua uma expressão, preservando curto-circuito."""
        # Retorno por classe: REG -> VReg | IMM12 -> valor do imediato |
        # POW2 -> expoente | COND e EFFECT -> None.
        # dst é só uma dica de destino (evita um Move em atribuições).
        choice = self._lookup(node, result_class)   # regra escolhida pelo Selector
        if choice is None:  # nenhuma regra cobre essa classe nesse nó
            raise SelectionError(
                f"sem cobertura {result_class.name} para "
                f"{type(node).__name__} ({_where(node)})"
            )
        if result_class is COND and (true_label is None or false_label is None):
            raise ValueError("COND exige os labels verdadeiro e falso")
        action = getattr(self, "_act_" + choice.rule.action, None)
        if action is None:  # a regra existe no catálogo, mas falta o emissor
            raise SelectionError(
                f"ação sem emissor: {choice.rule.action!r} "
                f"(regra {choice.rule.name!r}, {_where(node)})"
            )
        return action(node, choice, dst, true_label, false_label)
 
    def _solve(self, choice):
        """Reconstrua, em ordem, os subproblemas não-COND de uma Choice."""
        # avalia cada filho na classe exigida pela regra, da esquerda para a direita
        # (a ordem dos filhos é a ordem de avaliação da linguagem)
        return [self.expression(child, cls) for child, cls in choice.subproblems]
 
    def _out(self, dst):
        # usa o destino sugerido; senão cria um temp novo
        return dst if dst is not None else self.ctx.temp()
 
    def _emit(self, instruction):
        self.ctx.emit(instruction)

# Comandos ->

    def statement(self, node):
        """Traduza um comando ad hoc."""
        # Essa função verifica qual é o nó de comando da AST e baseado nele vai para o método correspondente
        if isinstance(node, Block): # trata bloco "{...}"
            for inner in node.statements: # chama statement recursivamente para cada comando interno
                self.statement(inner)
        elif isinstance(node, VarDecl):
            self._var_decl(node)
        elif isinstance(node, Assignment):
            self._store(
                node.target.metadata["symbol"], # símbolo da variavel destino
                node.value, # expressão do lado direito
                node.span # posição no fonte, usada como origem da instrução
                )
        elif isinstance(node, CallStmt):
            self._call_stmt(node)
        elif isinstance(node, IfStmt):
            self._if(node)
        elif isinstance(node, WhileStmt):
            self._while(node)
        elif isinstance(node, ReturnStmt):
            self._return(node)
        elif isinstance(node, PrintStmt):
            self._print(node)
        else:   # tipo de nó desconhecido: falha em vez de ignorar o comando
            raise NotImplementedError(
                f"geração não implementada para {type(node).__name__}"
            )
 
    def _store(self, symbol, value, origin):
        """Avalie ``value`` por completo e só então grave no VReg do símbolo."""
        # usado por declaração com inicializador e por atribuição
        target = self.ctx.variable(symbol)  # VReg da variável (achado pelo símbolo)
        result = self.expression(value, REG, dst=target)    # avalia o lado direito, sugerindo gravar direto em target
        if result != target:    # a expressão devolveu outro VReg (ex.: outra variável): copia
            self.ctx.emit(I.Move(target, result, origin=origin))
 
    def _var_decl(self, node):
        # Sem inicializador, o VReg não é escrito (nem zerado implicitamente).
        if node.initializer is not None:
            self._store(node.metadata["symbol"], node.initializer, node.span)
 
    def _call_stmt(self, node):
        call = node.call
        # Chamada como comando: resultado descartado. Se o catálogo não oferece
        # EFFECT (chamada não-void sem essa regra), avalia-se como REG.
        if self._lookup(call, EFFECT) is not None:
            self.expression(call, EFFECT)
        else:
            self.expression(call, REG)
 
    def _if(self, node):
        # Estrutura:
        #   <condição> -> then_label (verdadeiro) | else_label (falso)
        #   then_label: <then>  [jump end]
        #   else_label: <else>
        #   end_label:
        then_label = self.ctx.label()
        end_label = self.ctx.label()
        has_else = node.else_block is not None
        else_label = self.ctx.label() if has_else else end_label    # sem else, o falso vai direto ao fim
 
        self.expression(node.condition, COND, then_label, else_label)   # condição como desvio (COND)
        self._label(then_label)
        self.statement(node.then_block)
        if has_else:
            self.ctx.emit(I.Jump(end_label, origin=node.span))  # o then não pode "cair" dentro do else
            self._label(else_label)
            self.statement(node.else_block)
        self._label(end_label)
 
    def _while(self, node):
        # Estrutura:
        #   test_label: <condição> -> body_label (verdadeiro) | end_label (falso)
        #   body_label: <corpo>  jump test_label
        #   end_label:
        test_label = self.ctx.label()
        body_label = self.ctx.label()
        end_label = self.ctx.label()
 
        self._label(test_label)
        self.expression(node.condition, COND, body_label, end_label)    # a condição é reavaliada a cada volta
        self._label(body_label)
        self.statement(node.body)
        self.ctx.emit(I.Jump(test_label, origin=node.span))  # salto de volta ao teste
        self._label(end_label)
 
    def _return(self, node):
        if node.value is None:  # return; (função void)
            self.ctx.emit(I.Return(None, origin=node.span))
        else:
            value = self.expression(node.value, REG)    # valor retornado sempre em REG
            self.ctx.emit(I.Return(value, origin=node.span))
 
    def _print(self, node):
        # Cada item é processado por completo antes do seguinte.
        for item in node.items:
            if isinstance(item, StringLiteral): # texto: vai para o pool, sem ocupar VReg
                ref = self.ctx.string(item.value)
                self.ctx.emit(I.PrintString(ref, origin=item.span))
            elif isinstance(item, Expr):    # expressão: avalia e imprime conforme o tipo
                value = self.expression(item, REG)
                if item.metadata.get("type") is TypeName.BOOL:
                    self.ctx.emit(I.PrintBool(value, origin=item.span))
                else:
                    self.ctx.emit(I.PrintInt(value, origin=item.span))
            else:
                raise NotImplementedError(
                    f"item de print não suportado: {type(item).__name__}"
                )
        # todo print termina com uma quebra de linha
        newline = self.ctx.string("\n")
        self.ctx.emit(I.PrintString(newline, origin=node.span))
 
    def _label(self, label):
        # marca a posição do label no código
        self.ctx.emit(I.LabelInstr(label))

# Ações das regras (_act_<nome da ação no catálogo>) ->
# Todas recebem (node, choice, dst, t, f):
#   node   = nó da AST
#   choice = escolha do Selector (traz os subproblemas, já na ordem de avaliação)
#   dst    = VReg de destino sugerido (None -> cria um temporário)
#   t, f   = labels verdadeiro/falso (só usados pelas ações da classe COND)
# Ações REG devolvem o VReg com o resultado. Ações COND e EFFECT não devolvem nada.

# folhas ->
 
    def _act_load_integer(self, node, choice, dst, t, f):
        out = self._out(dst)
        self._emit(I.Li(out, signed_literal(node), origin=node.span))   # signed_literal já resolve o "-" de um -5
        return out
 
    def _act_load_bool(self, node, choice, dst, t, f):
        out = self._out(dst)
        self._emit(I.Li(out, 1 if node.value else 0, origin=node.span))    # bool é representado por 0/1
        return out
 
    def _act_identifier(self, node, choice, dst, t, f):
        # Reutiliza o VReg do símbolo: nenhuma instrução.
        return self.ctx.variable(node.metadata["symbol"])
 
    def _act_literal_imm12(self, node, choice, dst, t, f):
        # classe IMM12: não emite nada, só devolve o valor para o pai usar como imediato
        return signed_literal(node)
 
    def _act_literal_pow2(self, node, choice, dst, t, f):
        # classe POW2: devolve o expoente k (2^k), que vira o deslocamento do Slli
        return signed_literal(node).bit_length() - 1
 
# aritmética ->
# Padrão: _solve avalia os filhos (esquerda -> direita), cria o
# destino e emite uma instrução. Só muda a instrução e o que cada filho devolve.
 
    def _act_negate(self, node, choice, dst, t, f):
        (src,) = self._solve(choice)    # um único filho
        out = self._out(dst)
        self._emit(I.Neg(out, src, origin=node.span))
        return out
 
    def _arith(self, cls, node, choice, dst):
        # auxiliar das aritméticas gerais: cls é a instrução (Add, Sub, Mul, Div, Rem)
        lhs, rhs = self._solve(choice)
        out = self._out(dst)
        self._emit(cls(out, lhs, rhs, origin=node.span))
        return out
 
    def _act_add(self, node, choice, dst, t, f):
        return self._arith(I.Add, node, choice, dst)
 
    def _act_subtract(self, node, choice, dst, t, f):
        return self._arith(I.Sub, node, choice, dst)
 
    def _act_multiply(self, node, choice, dst, t, f):
        return self._arith(I.Mul, node, choice, dst)
 
    def _act_divide(self, node, choice, dst, t, f):
        return self._arith(I.Div, node, choice, dst)
 
    def _act_remainder(self, node, choice, dst, t, f):
        return self._arith(I.Rem, node, choice, dst)
 
    def _act_addi_right(self, node, choice, dst, t, f):
        src, imm = self._solve(choice)  # x + c : filhos são (REG, IMM12)
        out = self._out(dst)
        self._emit(I.Addi(out, src, imm, origin=node.span))
        return out
 
    def _act_addi_left(self, node, choice, dst, t, f):
        imm, src = self._solve(choice)  # c + x : a ordem dos filhos é invertida (IMM12, REG)
        out = self._out(dst)
        self._emit(I.Addi(out, src, imm, origin=node.span))
        return out
 
    def _act_subtract_imm12(self, node, choice, dst, t, f):
        # x - c vira addi x, -c. O literal é reconhecido diretamente na regra,
        # sem classe IMM12. x é avaliado uma única vez.
        src = self.expression(node.left, REG)   # só o lado esquerdo é avaliado; o literal vem direto do nó
        out = self._out(dst)
        self._emit(I.Addi(out, src, -signed_literal(node.right), origin=node.span))   # imediato oposto
        return out
 
    def _act_shift_right_operand(self, node, choice, dst, t, f):
        src, shift = self._solve(choice)    # x * 2^k : filhos são (REG, POW2), POW2 devolve k
        out = self._out(dst)
        self._emit(I.Slli(out, src, shift, origin=node.span))
        return out
 
    def _act_shift_left_operand(self, node, choice, dst, t, f):
        shift, src = self._solve(choice)    # 2^k * x : ordem invertida (POW2, REG)
        out = self._out(dst)
        self._emit(I.Slli(out, src, shift, origin=node.span))
        return out
 
# comparações como valor (REG) ->
# Resultado materializado em um VReg como 0/1. O RV64 só tem Slt (<), então os
# outros operadores são montados com troca de operandos e/ou Seqz/Snez.
 
    def _act_less_than(self, node, choice, dst, t, f):
        lhs, rhs = self._solve(choice)
        out = self._out(dst)
        self._emit(I.Slt(out, lhs, rhs, origin=node.span))  # a < b
        return out
 
    def _act_greater_than(self, node, choice, dst, t, f):
        lhs, rhs = self._solve(choice)
        out = self._out(dst)
        self._emit(I.Slt(out, rhs, lhs, origin=node.span))  # a > b  ==  b < a
        return out
 
    def _act_less_equal(self, node, choice, dst, t, f):
        # a <= b  ==  !(b < a)
        lhs, rhs = self._solve(choice)
        out = self._out(dst)
        self._emit(I.Slt(out, rhs, lhs, origin=node.span))
        self._emit(I.Seqz(out, out, origin=node.span))  # inverte o 0/1 (lê e escreve out)
        return out
 
    def _act_greater_equal(self, node, choice, dst, t, f):
        # a >= b  ==  !(a < b)
        lhs, rhs = self._solve(choice)
        out = self._out(dst)
        self._emit(I.Slt(out, lhs, rhs, origin=node.span))
        self._emit(I.Seqz(out, out, origin=node.span))
        return out
 
    def _act_equal(self, node, choice, dst, t, f):
        lhs, rhs = self._solve(choice)
        out = self._out(dst)
        self._emit(I.Xor(out, lhs, rhs, origin=node.span))  # xor é 0 só quando os valores são iguais
        self._emit(I.Seqz(out, out, origin=node.span))      # igual -> 1
        return out
 
    def _act_not_equal(self, node, choice, dst, t, f):
        lhs, rhs = self._solve(choice)
        out = self._out(dst)
        self._emit(I.Xor(out, lhs, rhs, origin=node.span))
        self._emit(I.Snez(out, out, origin=node.span))      # diferente -> 1
        return out
 
    def _act_logical_not_reg(self, node, choice, dst, t, f):
        (src,) = self._solve(choice)
        out = self._out(dst)
        self._emit(I.Seqz(out, src, origin=node.span))  # !x : vira 1 se x é 0, e 0 caso contrário
        return out
 
# comparações como condição (COND) ->
# Em vez de produzir 0/1, desviam direto: Branch para o label verdadeiro e
# Jump para o falso. Isso é o que o if e o while usam.
 
    def _branch(self, cls, node, choice, true_label, false_label, swap=False):
        # auxiliar: cls é o desvio (Blt, Bge, Beq, Bne); swap troca os operandos
        lhs, rhs = self._solve(choice)
        if swap:
            lhs, rhs = rhs, lhs
        self._emit(cls(lhs, rhs, true_label, origin=node.span))  # desvia se a relação vale
        self._emit(I.Jump(false_label, origin=node.span))        # senão, vai para o falso
 
    def _act_branch_less_than(self, node, choice, dst, t, f):
        self._branch(I.Blt, node, choice, t, f)                 # a < b
 
    def _act_branch_greater_than(self, node, choice, dst, t, f):
        self._branch(I.Blt, node, choice, t, f, swap=True)      # a > b  ==  b < a
 
    def _act_branch_less_equal(self, node, choice, dst, t, f):
        self._branch(I.Bge, node, choice, t, f, swap=True)      # a <= b  ==  b >= a
 
    def _act_branch_greater_equal(self, node, choice, dst, t, f):
        self._branch(I.Bge, node, choice, t, f)                 # a >= b
 
    def _act_branch_equal(self, node, choice, dst, t, f):
        self._branch(I.Beq, node, choice, t, f)
 
    def _act_branch_not_equal(self, node, choice, dst, t, f):
        self._branch(I.Bne, node, choice, t, f)
 
    def _act_bool_literal_cond(self, node, choice, dst, t, f):
        self._emit(I.Jump(t if node.value else f, origin=node.span))    # true/false já decide o destino
 
    def _act_bool_identifier_cond(self, node, choice, dst, t, f):
        register = self.ctx.variable(node.metadata["symbol"])
        self._emit(I.Bnez(register, t, origin=node.span))   # variável bool != 0 -> verdadeiro
        self._emit(I.Jump(f, origin=node.span))
 
    def _act_logical_not_cond(self, node, choice, dst, t, f):
        # A negação só troca os destinos: nenhuma instrução própria.
        ((operand, cls),) = choice.subproblems
        self.expression(operand, cls, f, t)
 
# curto-circuito ->
# a && b : se a é falso, b nem é avaliado.  a || b : se a é verdadeiro, b nem é avaliado.
# Por isso o lado direito só é emitido depois de um label (middle), alcançado
# apenas quando o lado esquerdo não decidiu o resultado.
 
    def _short_circuit(self, choice, true_label, false_label, is_and):
        """Emita os dois filhos COND encadeando destinos, sem avaliar o direito
        fora do caminho em que o esquerdo não decidiu o resultado."""
        (left, left_cls), (right, right_cls) = choice.subproblems
        middle = self.ctx.label()   # início do lado direito
        if is_and:
            # && : esquerdo verdadeiro -> testa o direito | esquerdo falso -> resultado é falso
            self.expression(left, left_cls, middle, false_label)
        else:
            # || : esquerdo verdadeiro -> resultado é verdadeiro | esquerdo falso -> testa o direito
            self.expression(left, left_cls, true_label, middle)
        self._label(middle)
        self.expression(right, right_cls, true_label, false_label)  # o direito decide o resultado
 
    def _act_logical_and_cond(self, node, choice, dst, t, f):
        self._short_circuit(choice, t, f, is_and=True)
 
    def _act_logical_or_cond(self, node, choice, dst, t, f):
        self._short_circuit(choice, t, f, is_and=False)
 
    def _logical_reg(self, node, choice, dst, is_and):
        # Dois caminhos escrevem 1 ou 0 no mesmo destino e convergem.
        # Usado quando &&/|| precisa virar valor (ex.: bool x = a && b;).
        out = self._out(dst)
        true_label, false_label, end_label = (self.ctx.label() for _ in range(3))
        self._short_circuit(choice, true_label, false_label, is_and)    # decide entre os dois caminhos
        self._label(true_label)
        self._emit(I.Li(out, 1, origin=node.span))      # caminho verdadeiro: out = 1
        self._emit(I.Jump(end_label, origin=node.span)) # pula o caminho falso
        self._label(false_label)
        self._emit(I.Li(out, 0, origin=node.span))      # caminho falso: out = 0
        self._label(end_label)                          # os dois caminhos convergem aqui
        return out
 
    def _act_logical_and_reg(self, node, choice, dst, t, f):
        return self._logical_reg(node, choice, dst, is_and=True)
 
    def _act_logical_or_reg(self, node, choice, dst, t, f):
        return self._logical_reg(node, choice, dst, is_and=False)
 
# chamadas ->
 
    def _call(self, node, choice, out):
        # Argumentos avaliados da esquerda para a direita, uma vez cada.
        args = tuple(self._solve(choice))   # cada argumento vira um VReg (classe REG), em ordem
        self._emit(I.Call(out, node.metadata["symbol"], args, origin=node.span))    # out=None descarta o retorno
 
    def _act_call_reg(self, node, choice, dst, t, f):
        out = self._out(dst)    # chamada usada como valor
        self._call(node, choice, out)
        return out
 
    def _act_call_effect(self, node, choice, dst, t, f):
        self._call(node, choice, None)  # chamada como comando: sem destino, resultado descartado
 
    def _act_bool_call_cond(self, node, choice, dst, t, f):
        out = self.ctx.temp()   # função bool usada como condição: chama e depois desvia
        self._call(node, choice, out)
        self._emit(I.Bnez(out, t, origin=node.span))    # retorno != 0 -> verdadeiro
        self._emit(I.Jump(f, origin=node.span))