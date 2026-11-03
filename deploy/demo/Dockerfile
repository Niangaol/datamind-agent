ARG RUNTIME_IMAGE=datamind-demo-runtime:20261019-amd64
FROM ${RUNTIME_IMAGE}
WORKDIR /opt/app
RUN mkdir -p /opt/runtime-manifests \
    && python -m pip freeze > /opt/runtime-manifests/main.freeze.txt \
    && /opt/formula/bin/python3.12 -m pip freeze > /opt/runtime-manifests/formula.freeze.txt \
    && python -m pip check
COPY app/ /opt/app/
COPY model_cache/ /opt/app/.cache/
RUN python -c "import fastapi,uvicorn,psycopg2,sqlglot,pymupdf,fastembed,qdrant_client,rapidocr,cv2,docx,pptx,openpyxl,ezdxf; import datamind.api; print('Application imports verified')" \
    && /opt/formula/bin/python3.12 -c "from importlib.metadata import version; import numpy,cv2,onnxruntime,tokenizers; assert version('rapid-doc') == '0.9.10'; print('Formula imports verified')"
EXPOSE 8000
CMD ["python", "/opt/app/deploy/demo/entrypoint.py"]
