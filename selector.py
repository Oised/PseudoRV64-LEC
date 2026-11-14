"""Ponto de implementação do reconhecimento de padrões e custos mínimos."""

from patterns import *

def _match_pattern(node, pattern):
    """Retorna os subproblemas exigidos pelo padrão ou None se nada casar"""
    if pattern.kind is not None and not isinstance(node, pattern.kind): # se o tipo não corresponder return none
        return None

    if pattern.operator is not None: # se o operador não corresponder return none
        if getattr(node, "operator", None) != pattern.operator:
            return None

    if pattern.predicate is not None and pattern.predicate(node): # se o predicado não corresponder
        return None

    actual_children = children(node)
    pattern_children = pattern.children

    # predicados que não contem filhos podem reconher uma subárvore inteira,
    # como a negacão imediata de um literal

    if not pattern_children and pattern.predicate is not None: # se não houver filhos no padrão e houver um predicado, retorna uma tupla vazia
        return ()

    if any(isinstance(child_pattern, Args) for child_pattern in pattern_children): # se houver um Args no padrão, retorna uma tupla vazia
        if len(pattern_children) != 1 or not isinstance(pattern_children[0], Args): # se houver mais de um filho no padrão ou o único filho não for um Args, levanta um ValueError
            raise ValueError("Args deve ser o único filho do padrão")
        result_class = pattern_children[0].result # se houver um Args no padrão, retorna uma tupla com os filhos reais e a classe de resultado
        return tuple((child, result_class) for child in actual_children)

    if len(actual_children) != len(pattern_children): # se o número de filhos reais não corresponder ao número de filhos do padrão, retorna None
        return None

    subproblems = []
    for child, child_pattern in zip(actual_children, pattern_children): # para cada filho real
        if not isinstance(child_pattern, Need): # se o filho do padrão não for um Pattern, levanta um ValueError
            raise ValueError("filho do padrão deve ser Need ou Args")
        subproblems.append((child, child_pattern.result)) # adiciona o filho real e a classe de resultado à lista de subproblemas

    return tuple(subproblems) # retorna a tupla de subproblemas

class Selector:
    """Selecione coberturas sem executar expressões ou emitir instruções."""

    def __init__(self, rules):
        self.rules = tuple(rules)

    def analyze(self, expr):
        """Calcule mínimos por nó/classe sem emitir instruções."""
        table = SelectionTable()
        visited = set()
        visiting = set()

        def visit(node):  
            """Visite a AST e calcule os custos mínimos para cada nó/classe."""
            node_id = id(node)
            if node_id in visited:
                return
            if node_id in visiting: # detecta ciclos na AST, o que não deveria acontecer em uma árvore de sintaxe abstrata 
                raise ValueError("Ciclo detectado na AST")

            visiting.add(node_id) # adiciona o nó e itero sobre seus filhos para calcular os custos mínimos para cada nó/classe
            for child in children(node):
                visit(child)
            visiting.remove(node_id)

            best = {} 
            for rule in self.rules: # for para cada regra, tenta casar o padrão com o nó atual e calcula o custo total se casar
                subproblems = _match_pattern(node, rule.pattern) # pega os subproblemas exigidos pelo padrão ou None se nada casar,
                if subproblems is None:                          # se não casar, passa para a próxima regra
                    continue

                # Calcule o custo total se casar e verifique se todos os subproblemas foram resolvidos
                total_cost = rule.cost
                covered = True

                for child, result_class in subproblems: # para cada subproblema, procura a melhor escolha na tabela de seleção
                    choice = table.lookup(child, result_class) # result_class é a classe de resultado do padrão, que é usada para procurar a melhor escolha na tabela de seleção
                    if choice is None:
                        covered = False
                        break # paro se algum subproblema não tiver uma escolha na tabela de seleção
                    total_cost += choice.cost
                if not covered:
                    continue

                previous = best.get(rule.result) # pega a melhor escolha anterior para a classe de resultado do padrão, se houver
                if previous is None or total_cost < previous.cost:
                    best[rule.result] = Choice(
                        cost=total_cost,
                        rule=rule,
                        subproblems=subproblems
                    ) # se não houver uma melhor escolha anterior ou se o custo total for menor que o custo da melhor escolha anterior, atualiza a melhor escolha para a classe de resultado do padrão

            for result_class, choice in best.items(): # para cada melhor escolha, adiciona a escolha à tabela de seleção
                table.put(node, result_class, choice)

            visiting.remove(node_id)
            visited.add(node_id)

        visit(expr)
        return table