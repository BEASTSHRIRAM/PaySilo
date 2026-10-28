FROM node:20-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# BACKEND_URL is a build arg because next.config.mjs reads it at build time to bake in the /api rewrite target.
ARG BACKEND_URL=http://localhost:8000
ENV BACKEND_URL=$BACKEND_URL
RUN npm run build
EXPOSE 3000
CMD ["npm", "run", "start"]
