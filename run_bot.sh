#!/usr/bin/env bash
SESSION=mysession
WORKDIR=/home/ec2-user/final_3etapas

# 1) Crea la sesión tmux si no existe
tmux has-session -t "$SESSION" 2>/dev/null
if [ $? != 0 ]; then
  tmux new-session -d -s "$SESSION"
fi

# 2) Dentro de la sesión: bucle infinito que relanza el bot
tmux send-keys -t "$SESSION" "
  cd \"$WORKDIR\"
  while true; do
      echo \"[tmux] Lanzando bot...\"
      python3 main.py
      echo \"[tmux] Bot terminado. Reinicio en 5 s...\"
      sleep 5
  done
" C-m

echo "Bot corriendo en tmux:$SESSION  (adjunta con:  tmux attach -t $SESSION )"
